import torch
import torch.nn.functional as F
import pytest
import torch.multiprocessing as mp
from torch.utils.checkpoint import checkpoint
from torch.fx import GraphModule
from colossalai.fx import ColoTracer
import colossalai
from colossalai.utils import free_port
from colossalai.core import global_context as gpc
from colossalai.fx.graph_module import ColoGraphModule

try:
    from colossalai.fx.codegen import ActivationCheckpointCodeGen
    with_codegen = True
except:
    # fall back to older pytorch version
    from colossalai.fx.codegen import python_code_with_activation_checkpoint
    with_codegen = False


class MyModule(torch.nn.Module):

    def __init__(self):
        super().__init__()
        self.linear1 = torch.nn.Linear(4, 4)
        self.linear2 = torch.nn.Linear(4, 4)
        self.linear3 = torch.nn.Linear(4, 4)
        self.linear4 = torch.nn.Linear(4, 4)
        self.linear5 = torch.nn.Linear(4, 4)
        self.linear6 = torch.nn.Linear(4, 4)

    def forward(self, x):
        return self.linear6(self.linear5(self.linear4(self.linear3(self.linear2(self.linear1(x))))))


def _run_act_ckpt_codegen(rank):
    # launch colossalai to make sure we could execute colossalai.utils.checkpoint currectly
    colossalai.launch(config={}, rank=rank, world_size=1, host='localhost', port=free_port(), backend='nccl')

    # build model and run forward
    model = MyModule()
    data1 = torch.rand(4, 4)

    # copy model to cuda
    model = model.to(device="cuda")
    data1 = data1.to(device="cuda")

    non_fx_out = model(data1)

    # trace the module and replace codegen
    tracer = ColoTracer(trace_act_ckpt=True)
    graph = tracer.trace(model)
    codegen = ActivationCheckpointCodeGen()
    graph.set_codegen(codegen)

    # annotate nested checkpoint
    for node in graph.nodes:
        if node.name == "linear1":
            setattr(node, "activation_checkpoint", [0, 0, 0])
            continue
        if node.name == "linear2":
            setattr(node, "activation_checkpoint", [0, 0, None])
        if node.name == "linear3":
            setattr(node, "activation_checkpoint", [0, 0, 1])
        if node.name == "linear4":
            setattr(node, "activation_checkpoint", [0, 1, None])
        if node.name == "linear5":
            setattr(node, "activation_checkpoint", 1)
    gm = ColoGraphModule(model, graph)
    gm.recompile()

    # assert checkpoint function will be generated and
    code = graph.python_code('self').src
    assert 'colossalai.utils.activation_checkpoint.checkpoint(self.checkpoint_0_0, False, x, use_reentrant=False)' in code and \
    'colossalai.utils.activation_checkpoint.checkpoint(self.checkpoint_0_1, False, linear3, use_reentrant=False)' in code and \
    'colossalai.utils.activation_checkpoint.checkpoint(self.checkpoint_0_0_0, False, x, use_reentrant=False)' in code and \
    'colossalai.utils.activation_checkpoint.checkpoint(self.checkpoint_0_0_1, False, linear2, use_reentrant=False)' in code and \
    'colossalai.utils.activation_checkpoint.checkpoint(self.checkpoint_0, False, x, use_reentrant=False)' in code and \
    'colossalai.utils.activation_checkpoint.checkpoint(self.checkpoint_1, False, linear4, use_reentrant=False)' in code

    # recompile and verify the outputs are consistent
    fx_out = gm(data1)
    assert torch.equal(non_fx_out, fx_out)

    gpc.destroy()


@pytest.mark.skipif(not with_codegen, reason='torch version is lower than 1.12.0')
def test_act_ckpt_codegen():
    mp.spawn(_run_act_ckpt_codegen, nprocs=1)


def _run_act_ckpt_python_code_torch11(rank):
    # launch colossalai to make sure we could execute colossalai.utils.checkpoint currectly
    colossalai.launch(config={}, rank=rank, world_size=1, host='localhost', port=free_port(), backend='nccl')

    # build model and run forward
    model = MyModule()
    data1 = torch.rand(4, 4)

    # copy model to cuda
    model = model.to(device="cuda")
    data1 = data1.to(device="cuda")

    non_fx_out = model(data1)

    # trace the module and replace codegen
    tracer = ColoTracer(trace_act_ckpt=True)
    graph = tracer.trace(model)
    codegen = ActivationCheckpointCodeGen()
    graph.set_codegen(codegen)

    # annotate nested checkpoint
    for node in graph.nodes:
        if node.name == "linear1":
            setattr(node, "activation_checkpoint", [0, 0, 0])
            continue
        if node.name == "linear2":
            setattr(node, "activation_checkpoint", [0, 0, None])
        if node.name == "linear3":
            setattr(node, "activation_checkpoint", [0, 0, 1])
        if node.name == "linear4":
            setattr(node, "activation_checkpoint", [0, 1, None])
        if node.name == "linear5":
            setattr(node, "activation_checkpoint", 1)
    gm = ColoGraphModule(model, graph)
    gm.recompile()

    # assert checkpoint function will be generated and
    code = graph.python_code('self').src
    assert 'colossalai.utils.activation_checkpoint.checkpoint(self.checkpoint_0_0, False, x, use_reentrant=False)' in code and \
    'colossalai.utils.activation_checkpoint.checkpoint(self.checkpoint_0_1, False, linear3, use_reentrant=False)' in code and \
    'colossalai.utils.activation_checkpoint.checkpoint(self.checkpoint_0_0_0, False, x, use_reentrant=False)' in code and \
    'colossalai.utils.activation_checkpoint.checkpoint(self.checkpoint_0_0_1, False, linear2, use_reentrant=False)' in code and \
    'colossalai.utils.activation_checkpoint.checkpoint(self.checkpoint_0, False, x, use_reentrant=False)' in code and \
    'colossalai.utils.activation_checkpoint.checkpoint(self.checkpoint_1, False, linear4, use_reentrant=False)' in code

    # recompile and verify the outputs are consistent
    fx_out = gm(data1)
    assert torch.equal(non_fx_out, fx_out)

    gpc.destroy()


@pytest.mark.skipif(with_codegen, reason='torch version is equal to or higher than 1.12.0')
@pytest.mark.skip(reason="currently torch11 ColoGraphModule is not done")
def test_act_ckpt_python_code_torch11():
    mp.spawn(_run_act_ckpt_python_code_torch11, nprocs=1)


if __name__ == '__main__':
    _run_act_ckpt_codegen(rank=0)
