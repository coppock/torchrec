#!/usr/bin/env python3
# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.


from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Type

import torch
from fbgemm_gpu.split_table_batched_embeddings_ops_inference import (
    IntNBitTableBatchedEmbeddingBagsCodegen,
)
from torch import nn
from torchrec.distributed.embedding import (
    create_sharding_infos_by_sharding,
    EmbeddingShardingInfo,
)
from torchrec.distributed.embedding_sharding import EmbeddingSharding
from torchrec.distributed.embedding_types import (
    BaseQuantEmbeddingSharder,
    FeatureShardingMixIn,
    GroupedEmbeddingConfig,
    KJTList,
    ListOfKJTList,
    ShardingType,
)
from torchrec.distributed.fused_params import (
    FUSED_PARAM_QUANT_STATE_DICT_SPLIT_SCALE_BIAS,
    FUSED_PARAM_REGISTER_TBE_BOOL,
    get_tbes_to_register_from_iterable,
    is_fused_param_quant_state_dict_split_scale_bias,
    is_fused_param_register_tbe,
)
from torchrec.distributed.quant_state import ShardedQuantEmbeddingModuleState
from torchrec.distributed.sharding.rw_sequence_sharding import (
    InferRwSequenceEmbeddingSharding,
)
from torchrec.distributed.sharding.rw_sharding import InferRwSparseFeaturesDist
from torchrec.distributed.sharding.sequence_sharding import InferSequenceShardingContext
from torchrec.distributed.sharding.tw_sequence_sharding import (
    InferTwSequenceEmbeddingSharding,
)
from torchrec.distributed.types import ParameterSharding, ShardingEnv
from torchrec.modules.embedding_configs import (
    data_type_to_sparse_type,
    dtype_to_data_type,
    EmbeddingConfig,
)
from torchrec.quant.embedding_modules import (
    EmbeddingCollection as QuantEmbeddingCollection,
    MODULE_ATTR_QUANT_STATE_DICT_SPLIT_SCALE_BIAS,
)
from torchrec.sparse.jagged_tensor import JaggedTensor, KeyedJaggedTensor
from torchrec.streamable import Multistreamable

torch.fx.wrap("len")

try:
    torch.ops.load_library("//deeplearning/fbgemm/fbgemm_gpu:sparse_ops")
    torch.ops.load_library("//deeplearning/fbgemm/fbgemm_gpu:sparse_ops_cpu")
except OSError:
    pass


@dataclass
class EmbeddingCollectionContext(Multistreamable):
    sharding_contexts: List[InferSequenceShardingContext]

    def record_stream(self, stream: torch.cuda.streams.Stream) -> None:
        for ctx in self.sharding_contexts:
            ctx.record_stream(stream)


def create_infer_embedding_sharding(
    sharding_type: str,
    sharding_infos: List[EmbeddingShardingInfo],
    env: ShardingEnv,
    device: Optional[torch.device] = None,
) -> EmbeddingSharding[
    InferSequenceShardingContext,
    KJTList,
    List[torch.Tensor],
    List[torch.Tensor],
]:
    if sharding_type == ShardingType.TABLE_WISE.value:
        return InferTwSequenceEmbeddingSharding(sharding_infos, env, device)
    elif sharding_type == ShardingType.ROW_WISE.value:
        return InferRwSequenceEmbeddingSharding(sharding_infos, env, device)
    else:
        raise ValueError(f"Sharding type not supported {sharding_type}")


@torch.fx.wrap
def _fx_unwrap_optional_tensor(optional: Optional[torch.Tensor]) -> torch.Tensor:
    assert optional is not None, "Expected optional to be non-None Tensor"
    return optional


def _construct_jagged_tensors_tw(
    embeddings: List[torch.Tensor],
    features: KJTList,
    need_indices: bool,
) -> Dict[str, JaggedTensor]:
    ret: Dict[str, JaggedTensor] = {}
    for i in range(len(embeddings)):
        embeddings_i: torch.Tensor = embeddings[i]
        features_i: KeyedJaggedTensor = features[i]

        lengths = features_i.lengths().view(-1, features_i.stride())
        values = features_i.values()
        length_per_key = features_i.length_per_key()

        embeddings_list = torch.split(embeddings_i, length_per_key, dim=0)
        stride = features_i.stride()
        lengths_tuple = torch.unbind(lengths.view(-1, stride), dim=0)
        if need_indices:
            values_list = torch.split(values, length_per_key)
            for i, key in enumerate(features_i.keys()):
                ret[key] = JaggedTensor(
                    lengths=lengths_tuple[i],
                    values=embeddings_list[i],
                    weights=values_list[i],
                )
        else:
            for i, key in enumerate(features_i.keys()):
                ret[key] = JaggedTensor(
                    lengths=lengths_tuple[i],
                    values=embeddings_list[i],
                    weights=None,
                )
    return ret


def _construct_jagged_tensors_rw(
    embeddings: List[torch.Tensor],
    features_before_input_dist: KeyedJaggedTensor,
    need_indices: bool,
    unbucketize_tensor: torch.Tensor,
) -> Dict[str, JaggedTensor]:
    ret: Dict[str, JaggedTensor] = {}
    unbucketized_embs = torch.concat(embeddings, dim=0).index_select(
        0, unbucketize_tensor
    )
    embs_split_per_key = unbucketized_embs.split(
        features_before_input_dist.length_per_key(), dim=0
    )
    stride = features_before_input_dist.stride()
    lengths_list = torch.unbind(
        features_before_input_dist.lengths().view(-1, stride), dim=0
    )
    values_list: List[torch.Tensor] = []
    if need_indices:
        # pyre-ignore
        values_list = torch.split(
            features_before_input_dist.values(),
            features_before_input_dist.length_per_key(),
        )
    for i, key in enumerate(features_before_input_dist.keys()):
        ret[key] = JaggedTensor(
            values=embs_split_per_key[i],
            lengths=lengths_list[i],
            weights=values_list[i] if need_indices else None,
        )
    return ret


def _construct_jagged_tensors(
    embeddings: List[torch.Tensor],
    features: KJTList,
    embedding_names_per_rank: List[List[str]],
    features_before_input_dist: KeyedJaggedTensor,
    need_indices: bool,
    unbucketize_tensor: Optional[torch.Tensor],
    features_to_permute_indices: Dict[str, torch.Tensor],
) -> Dict[str, JaggedTensor]:
    if unbucketize_tensor is not None:
        # RW sharding
        return _construct_jagged_tensors_rw(
            embeddings,
            features_before_input_dist,
            need_indices,
            unbucketize_tensor,
        )

    return _construct_jagged_tensors_tw(embeddings, features, need_indices)


@torch.fx.wrap
def output_jt_dict(
    emb_per_sharding: List[List[torch.Tensor]],
    features_per_sharding: List[KJTList],
    embedding_names_per_rank_per_sharding: List[List[List[str]]],
    need_indices: bool,
    features_before_input_dist_per_sharding: List[KeyedJaggedTensor],
    features_to_permute_indices: Dict[str, torch.Tensor],
    unbucketize_tensors: List[torch.Tensor],
    unbucketize_tensor_idxs_per_sharding: List[int],
) -> Dict[str, JaggedTensor]:
    jt_dict: Dict[str, JaggedTensor] = {}
    for (
        emb_sharding,
        features_sharding,
        embedding_names_per_rank,
        unbucketize_tensor_idx,
        features_before_input_dist,
    ) in zip(
        emb_per_sharding,
        features_per_sharding,
        embedding_names_per_rank_per_sharding,
        unbucketize_tensor_idxs_per_sharding,
        features_before_input_dist_per_sharding,
    ):
        jt_dict.update(
            _construct_jagged_tensors(
                embeddings=emb_sharding,
                features=features_sharding,
                embedding_names_per_rank=embedding_names_per_rank,
                features_before_input_dist=features_before_input_dist,
                need_indices=need_indices,
                unbucketize_tensor=unbucketize_tensors[unbucketize_tensor_idx]
                if unbucketize_tensor_idx != -1
                else None,
                features_to_permute_indices=features_to_permute_indices,
            )
        )
    return jt_dict


class ShardedQuantEmbeddingCollection(
    ShardedQuantEmbeddingModuleState[
        ListOfKJTList,
        List[List[torch.Tensor]],
        Dict[str, JaggedTensor],
        EmbeddingCollectionContext,
    ],
):
    """
    Sharded implementation of `QuantEmbeddingCollection`.
    """

    def __init__(
        self,
        module: QuantEmbeddingCollection,
        table_name_to_parameter_sharding: Dict[str, ParameterSharding],
        env: ShardingEnv,
        fused_params: Optional[Dict[str, Any]] = None,
        device: Optional[torch.device] = None,
    ) -> None:
        super().__init__()

        self._embedding_configs: List[EmbeddingConfig] = module.embedding_configs()

        sharding_type_to_sharding_infos = create_sharding_infos_by_sharding(
            module, table_name_to_parameter_sharding, fused_params
        )
        self._sharding_type_to_sharding: Dict[
            str,
            EmbeddingSharding[
                InferSequenceShardingContext,
                KJTList,
                List[torch.Tensor],
                List[torch.Tensor],
            ],
        ] = {
            sharding_type: create_infer_embedding_sharding(
                sharding_type, embedding_confings, env
            )
            for sharding_type, embedding_confings in sharding_type_to_sharding_infos.items()
        }
        self._embedding_dim: int = module.embedding_dim()
        self._local_embedding_dim: int = self._embedding_dim
        self._embedding_names_per_sharding: List[List[str]] = []
        self._embedding_names_per_rank_per_sharding: List[List[List[str]]] = []
        for sharding in self._sharding_type_to_sharding.values():
            self._embedding_names_per_sharding.append(sharding.embedding_names())
            self._embedding_names_per_rank_per_sharding.append(
                sharding.embedding_names_per_rank()
            )
        self._features_to_permute_indices: Dict[str, torch.Tensor] = {}

        self._device = device
        self._input_dists: List[nn.Module] = []
        self._lookups: List[nn.Module] = []
        self._create_lookups(fused_params, device)
        self._output_dists: List[nn.Module] = []

        self._feature_splits: List[int] = []
        self._features_order: List[int] = []

        self._has_uninitialized_input_dist: bool = True
        self._has_uninitialized_output_dist: bool = True

        self._embedding_dim: int = module.embedding_dim()
        self._need_indices: bool = module.need_indices()

        self._fused_params = fused_params

        tbes: Dict[
            IntNBitTableBatchedEmbeddingBagsCodegen, GroupedEmbeddingConfig
        ] = get_tbes_to_register_from_iterable(self._lookups)

        # Optional registration of TBEs for model post processing utilities
        if is_fused_param_register_tbe(fused_params):
            self.tbes: torch.nn.ModuleList = torch.nn.ModuleList(tbes.keys())

        quant_state_dict_split_scale_bias = (
            is_fused_param_quant_state_dict_split_scale_bias(fused_params)
        )

        if quant_state_dict_split_scale_bias:
            self._initialize_torch_state(
                tbes=tbes,
                table_name_to_parameter_sharding=table_name_to_parameter_sharding,
                tables_weights_prefix="embeddings",
            )
        else:
            table_wise_sharded_only: bool = all(
                [
                    sharding_type == ShardingType.TABLE_WISE.value
                    for sharding_type in self._sharding_type_to_sharding.keys()
                ]
            )
            assert (
                table_wise_sharded_only
            ), "ROW_WISE,COLUMN_WISE shardings can be used only in 'quant_state_dict_split_scale_bias' mode, specify fused_params[FUSED_PARAM_QUANT_STATE_DICT_SPLIT_SCALE_BIAS]=True to __init__ argument"

            self.embeddings: nn.ModuleDict = nn.ModuleDict()
            for table in self._embedding_configs:
                self.embeddings[table.name] = torch.nn.Module()

            for _sharding_type, lookup in zip(
                self._sharding_type_to_sharding.keys(), self._lookups
            ):
                lookup_state_dict = lookup.state_dict()
                for key in lookup_state_dict:
                    if key.endswith(".weight"):
                        table_name = key[: -len(".weight")]
                        self.embeddings[table_name].register_buffer(
                            "weight", lookup_state_dict[key]
                        )

    def _create_input_dist(
        self,
        input_feature_names: List[str],
        device: torch.device,
        input_dist_device: Optional[torch.device] = None,
    ) -> None:
        feature_names: List[str] = []
        self._feature_splits: List[int] = []
        for sharding in self._sharding_type_to_sharding.values():
            self._input_dists.append(
                sharding.create_input_dist(device=input_dist_device)
            )
            feature_names.extend(sharding.feature_names())
            self._feature_splits.append(len(sharding.feature_names()))
        self._features_order: List[int] = []
        for f in feature_names:
            self._features_order.append(input_feature_names.index(f))
        self._features_order = (
            []
            if self._features_order == list(range(len(self._features_order)))
            else self._features_order
        )
        self.register_buffer(
            "_features_order_tensor",
            torch.tensor(self._features_order, device=device, dtype=torch.int32),
            persistent=False,
        )

    def _create_lookups(
        self,
        fused_params: Optional[Dict[str, Any]],
        device: Optional[torch.device] = None,
    ) -> None:
        for sharding in self._sharding_type_to_sharding.values():
            self._lookups.append(
                sharding.create_lookup(fused_params=fused_params, device=device)
            )

    def _create_output_dist(
        self,
        device: Optional[torch.device] = None,
    ) -> None:
        for sharding in self._sharding_type_to_sharding.values():
            self._output_dists.append(sharding.create_output_dist(device))

    # pyre-ignore [14]
    # pyre-ignore
    def input_dist(
        self,
        ctx: EmbeddingCollectionContext,
        features: KeyedJaggedTensor,
    ) -> ListOfKJTList:
        if self._has_uninitialized_input_dist:
            self._create_input_dist(
                input_feature_names=features.keys() if features is not None else [],
                device=features.device(),
                input_dist_device=self._device,
            )
            self._has_uninitialized_input_dist = False
        if self._has_uninitialized_output_dist:
            self._create_output_dist(features.device())
            self._has_uninitialized_output_dist = False
        ret: List[KJTList] = []
        with torch.no_grad():
            features_by_sharding = []
            if self._features_order:
                features = features.permute(
                    self._features_order,
                    self._features_order_tensor,
                )
            features_by_sharding = features.split(
                self._feature_splits,
            )

            for i in range(len(self._input_dists)):
                input_dist = self._input_dists[i]
                input_dist_result = input_dist.forward(features_by_sharding[i])
                ret.append(input_dist_result)
                ctx.sharding_contexts.append(
                    InferSequenceShardingContext(
                        features=input_dist_result,
                        features_before_input_dist=features,
                        unbucketize_permute_tensor=input_dist.unbucketize_permute_tensor
                        if isinstance(input_dist, InferRwSparseFeaturesDist)
                        else None,
                    )
                )
        return ListOfKJTList(ret)

    def compute(
        self, ctx: EmbeddingCollectionContext, dist_input: ListOfKJTList
    ) -> List[List[torch.Tensor]]:
        ret: List[List[torch.Tensor]] = []

        for lookup, features in zip(
            self._lookups,
            dist_input,
        ):
            ret.append(
                [o.view(-1, self._embedding_dim) for o in lookup.forward(features)]
            )
        return ret

    # pyre-ignore
    def output_dist(
        self, ctx: EmbeddingCollectionContext, output: List[List[torch.Tensor]]
    ) -> Dict[str, JaggedTensor]:
        emb_per_sharding: List[List[torch.Tensor]] = []
        features_before_input_dist_per_sharding: List[KeyedJaggedTensor] = []
        features_per_sharding: List[KJTList] = []
        unbucketize_tensor_idxs_per_sharding: List[int] = []
        unbucketize_tensors: List[torch.Tensor] = []
        for sharding_output_dist, embeddings, sharding_ctx in zip(
            self._output_dists,
            output,
            ctx.sharding_contexts,
        ):
            sharding_output_dist_res: List[torch.Tensor] = sharding_output_dist.forward(
                embeddings, sharding_ctx
            )
            emb_per_sharding.append(sharding_output_dist_res)
            features_per_sharding.append(sharding_ctx.features)
            if sharding_ctx.unbucketize_permute_tensor is None:
                unbucketize_tensor_idxs_per_sharding.append(-1)
            else:
                unbucketize_tensors.append(
                    _fx_unwrap_optional_tensor(sharding_ctx.unbucketize_permute_tensor)
                )
                unbucketize_tensor_idxs_per_sharding.append(
                    len(unbucketize_tensors) - 1
                )
            features_before_input_dist_per_sharding.append(
                # pyre-ignore
                sharding_ctx.features_before_input_dist
            )
        return output_jt_dict(
            emb_per_sharding=emb_per_sharding,
            features_per_sharding=features_per_sharding,
            embedding_names_per_rank_per_sharding=self._embedding_names_per_rank_per_sharding,
            need_indices=self._need_indices,
            features_before_input_dist_per_sharding=features_before_input_dist_per_sharding,
            unbucketize_tensor_idxs_per_sharding=unbucketize_tensor_idxs_per_sharding,
            unbucketize_tensors=unbucketize_tensors,
            features_to_permute_indices=self._features_to_permute_indices,
        )

    # pyre-ignore
    def compute_and_output_dist(
        self, ctx: EmbeddingCollectionContext, input: ListOfKJTList
    ) -> Dict[str, JaggedTensor]:
        return self.output_dist(ctx, self.compute(ctx, input))

    # pyre-ignore
    def forward(self, *input, **kwargs) -> Dict[str, JaggedTensor]:
        ctx = self.create_context()
        dist_input = self.input_dist(ctx, *input, **kwargs)
        return self.compute_and_output_dist(ctx, dist_input)

    def copy(self, device: torch.device) -> nn.Module:
        if self._has_uninitialized_output_dist:
            self._create_output_dist(device)
            self._has_uninitialized_output_dist = False
        return super().copy(device)

    def create_context(self) -> EmbeddingCollectionContext:
        return EmbeddingCollectionContext(sharding_contexts=[])

    @property
    def shardings(self) -> Dict[str, FeatureShardingMixIn]:
        # pyre-ignore [7]
        return self._sharding_type_to_sharding


class QuantEmbeddingCollectionSharder(
    BaseQuantEmbeddingSharder[QuantEmbeddingCollection]
):
    """
    This implementation uses non-fused EmbeddingCollection
    """

    def shard(
        self,
        module: QuantEmbeddingCollection,
        params: Dict[str, ParameterSharding],
        env: ShardingEnv,
        device: Optional[torch.device] = None,
    ) -> ShardedQuantEmbeddingCollection:
        fused_params = self.fused_params if self.fused_params else {}
        fused_params["output_dtype"] = data_type_to_sparse_type(
            dtype_to_data_type(module.output_dtype())
        )
        if FUSED_PARAM_QUANT_STATE_DICT_SPLIT_SCALE_BIAS not in fused_params:
            fused_params[FUSED_PARAM_QUANT_STATE_DICT_SPLIT_SCALE_BIAS] = getattr(
                module, MODULE_ATTR_QUANT_STATE_DICT_SPLIT_SCALE_BIAS, False
            )
        if FUSED_PARAM_REGISTER_TBE_BOOL not in fused_params:
            fused_params[FUSED_PARAM_REGISTER_TBE_BOOL] = getattr(
                module, FUSED_PARAM_REGISTER_TBE_BOOL, False
            )
        return ShardedQuantEmbeddingCollection(
            module=module,
            table_name_to_parameter_sharding=params,
            env=env,
            fused_params=fused_params,
            device=device,
        )

    @property
    def module_type(self) -> Type[QuantEmbeddingCollection]:
        return QuantEmbeddingCollection
