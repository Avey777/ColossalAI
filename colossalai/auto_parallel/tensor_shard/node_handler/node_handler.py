from abc import ABC, abstractmethod
from typing import Dict, List, Union

import torch
from torch.fx.node import Node

from colossalai.auto_parallel.tensor_shard.sharding_strategy import (
    OperationData,
    ShardingStrategy,
    StrategiesVector,
    TrainCycleItem,
)
from colossalai.auto_parallel.tensor_shard.utils import check_sharding_spec_validity
from colossalai.device.device_mesh import DeviceMesh
from colossalai.tensor.shape_consistency import ShapeConsistencyManager

from .strategy import StrategyGenerator


class NodeHandler(ABC):
    '''
    The NodeHandler is an abstract class used to generate every possible strategies for an operator node.

    Args:
        node (Node): the input node in node argument list.
        device_mesh (DeviceMesh): A logical view of a physical mesh.
        strategies_vector (StrategiesVector): all the strategies generated in this handler will be recorded into the strategies_vector.
    '''

    def __init__(
        self,
        node: Node,
        device_mesh: DeviceMesh,
        strategies_vector: StrategiesVector,
    ) -> None:
        self.node = node
        self.predecessor_node = list(node._input_nodes.keys())
        self.successor_node = list(node.users.keys())
        self.device_mesh = device_mesh
        self.strategies_vector = strategies_vector

    def update_resharding_cost(self, strategy: ShardingStrategy) -> None:
        """
        Compute the resharding costs and save the costs in the ShardingStrategy object.
        """
        # TODO: test this function when other handlers are ready
        resharding_costs = {}
        shape_consistency_manager = ShapeConsistencyManager()

        for node in self.predecessor_node:
            node_name = str(node)

            # get the sharding specs for this node generated
            # in its own node handler
            assert hasattr(node, 'strategies_vector'), \
                f'The predecessor node {node_name} has no strategy vector to compute the resharding cost.'
            prev_strategy_vector = node.strategies_vector
            prev_sharding_specs = [
                prev_strategy.get_sharding_spec_by_name(node_name) for prev_strategy in prev_strategy_vector
            ]

            # get the current sharding spec generated by this node handler
            op_data = strategy.get_op_data_by_name(node_name)
            current_sharding_spec = strategy.sharding_specs[op_data]

            # create data structrure to store costs
            if op_data not in resharding_costs:
                resharding_costs[node] = []

            # for each sharding spec generated by the predecessor's node handler
            # compute the resharding cost to switch to the sharding spec generated
            # by the current node handler
            for prev_sharding_spec in prev_sharding_specs:
                _, _, resharding_cost = shape_consistency_manager.shape_consistency(prev_sharding_spec,
                                                                                    current_sharding_spec)
                resharding_cost = TrainCycleItem(fwd=resharding_cost["forward"],
                                                 bwd=resharding_cost["backward"],
                                                 total=resharding_cost["total"])
                resharding_costs[node].append(resharding_cost)
        strategy.resharding_costs = resharding_costs
        return strategy

    def register_strategy(self, compute_resharding_cost: bool = True) -> StrategiesVector:
        """
        Register different sharding strategies for the current node.
        """
        strategy_generators = self.get_strategy_generator()
        for generator in strategy_generators:
            strategies = generator.generate()

            # postprocess a strategy
            # postprocess can produce one strategy or multiple strategies
            post_processed_strategies_map = map(self.post_process, strategies)
            post_processed_strategies = []

            for strategy in post_processed_strategies_map:
                if isinstance(strategy, (list, tuple)):
                    post_processed_strategies.extend(strategy)
                else:
                    post_processed_strategies.append(strategy)

            # compute the resharding costs based on the previous node
            # strategies if specified
            if compute_resharding_cost:
                updated_strategies = map(self.update_resharding_cost, post_processed_strategies)
                post_processed_strategies = list(updated_strategies)

            self.strategies_vector.extend(post_processed_strategies)

        # validating the correctness of the sharding strategy
        for strategy in self.strategies_vector:
            for op_data, sharding_spec in strategy.sharding_specs.items():
                if op_data.data is not None and isinstance(op_data.data, torch.Tensor):
                    check_sharding_spec_validity(sharding_spec, op_data.data)

        return self.strategies_vector

    def post_process(self, strategy: ShardingStrategy) -> Union[ShardingStrategy, List[ShardingStrategy]]:
        # tranform the strategy generated
        # e.g. to process the sharding strategy for the transposed weights
        return strategy

    @abstractmethod
    def get_strategy_generator(self) -> List[StrategyGenerator]:
        """
        Define which generators should be used by this NodeHandler object.
        """
        pass

    @abstractmethod
    def get_operation_data_mapping(self) -> Dict[str, OperationData]:
        """
        Returns the mapping between the logical operation data to its physical data.
        A logical operation data is a data associated with an operation, which can be input and output. It is
        defined by the strategy generator, for example, a matrix multiplication operation has two operands "input"
        and "other" and one result "output". For a nn.Linear module, the physical operand for "input" is
        the module input, the physical operand for "other" is the module weight, and the physical result for "output"
        is the module output.
        Note that the operand name is specified by the StrategyGenerator object.

        For example:

            # for a linear layer
            mapping = {
                "input": Operand(name=str(self.node.args[0]), type=OperationDataType.ARG, data=self.node.args[0]._meta_data),
                "other": Operand(name="weight", type=OperationDataType.PARAM, data=self.named_parameters['weight']),
                "bias": Operand(name="bias", type=OperationDataType.PARAM, data=self.named_parameters['bias']),
                "output": Operand(name=str(self.node), type=OperationDataType.OUTPUT, data=self.node._meta_data),
            }
        """
        pass


class ModuleHandler(NodeHandler):

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)

        # set attributes to access module parameters for convenience
        assert self.node.graph.owning_module is not None, \
            f'The graph is not associated with a module, please make sure it can be used to instantiate a GraphModule object.'
        module = self.node.graph.owning_module.get_submodule(self.node.target)
        named_parameters = list(module.named_parameters(recurse=False))
        named_buffers = list(module.named_buffers(recurse=False))
        # convert named parameters from list to dict
        named_parameters = {k: v for k, v in named_parameters}
        named_buffers = {k: v for k, v in named_buffers}
        self.module = module
        self.named_parameters = named_parameters
        self.named_buffers = named_buffers
