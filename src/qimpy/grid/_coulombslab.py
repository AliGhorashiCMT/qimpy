from __future__ import annotations

import numpy as np
import torch

from qimpy import log, rc, grid
from . import Grid, FieldH

class Coulomb_Slab:

    def __init__(self, grid: Grid, n_ions: int, iDir: int) -> None:
        self.iDir = iDir
        self.grid = grid
        self.update_lattice_dependent(n_ions)

    def update_lattice_dependent(self, n_ions: int) -> None:
        grid = self.grid
        iDir = self.iDir

        Rsq = (self.grid.lattice.Rbasis).square().sum(dim=0)
        hlfL = torch.sqrt(Rsq[self.iDir])
        iG = grid.get_mesh("H").to(torch.double)
        Gsqi = (iG @ grid.lattice.Gbasis.T).square()
        Gsq = Gsqi.sum(dim=-1)
        Gplane = torch.sqrt(Gsq - Gsqi[..., iDir])
        self._kernel = torch.where(Gsq == 0.0, -0.5*hlfL**2, (4*np.pi) * (1 - torch.exp(-Gplane*hlfL) * torch.cos(np.pi*iG[..., iDir]))/Gsq)

    def __call__() -> None:
        pass

    def ewald() -> None:
        pass

    def stress() -> None:
        pass