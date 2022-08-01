import torch
from torch.nn import Module, Linear
from utils import remote_module

@remote_module
class LReg(Module):
    def __init__(self):
        super().__init__()
        self.fc1 = Linear(1, 1, bias=False)

    def forward(self, x):
        return self.fc1(x)

if __name__ == '__main__':
    script = torch.jit.script(LReg())
    torch.jit.save(script, "tests/lreg.pt")