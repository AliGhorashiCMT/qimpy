from __future__ import annotations

import numpy as np
import torch

from qimpy import TreeNode, rc
from qimpy.io import CheckpointPath
from qimpy.transport.material import Material
from . import Advect, BicubicPatch, parse_svg


class Geometry(TreeNode):
    """Geometry specification."""

    patches: list[Advect]  #: Advection for each quad patch
    adjacency: np.ndarray  #: as defined in `PatchSet`
    displacements: torch.Tensor  #: edge displacements for each adjacency

    # Constants for edge data transfer:
    IN_SLICES = [
        (slice(None), Advect.GHOST_L),
        (Advect.GHOST_R, slice(None)),
        (slice(None), Advect.GHOST_R),
        (Advect.GHOST_L, slice(None)),
    ]  #: input slice for each edge orientation during edge communication

    OUT_SLICES = [
        (Advect.NON_GHOST, Advect.GHOST_L),
        (Advect.GHOST_R, Advect.NON_GHOST),
        (Advect.NON_GHOST, Advect.GHOST_R),
        (Advect.GHOST_L, Advect.NON_GHOST),
    ]  #: output slice for each edge orientation during edge communication

    FLIP_DIMS = [(0, 1), (0,), None, (1,)]  #: which dims to flip during edge transfer

    # v_F and N_theta should eventually be material paramteters
    def __init__(
        self,
        *,
        material: Material,
        svg_file: str,
        N: tuple[int, int],
        checkpoint_in: CheckpointPath = CheckpointPath(),
    ):
        """
        Initialize geometry parameters.

        Parameters
        ----------
        svg_file
            :yaml:`Path to an SVG file containing the input geometry.
        """
        super().__init__()
        vertices, edges, quads, adjacency = parse_svg(svg_file)
        self.adjacency = adjacency.to(rc.cpu).numpy()
        self.displacements = get_displacements(vertices, edges, quads, adjacency)

        # Build an advect object for each quad
        self.patches = []
        v = material.transport_velocity
        for i_quad, quad in enumerate(quads):
            boundary = vertices[edges[quad, :-1].flatten()]
            transformation = BicubicPatch(boundary=boundary)
            self.patches.append(Advect(transformation=transformation, v=v, N=N))
        self.dt = min(patch.dt_max for patch in self.patches)

    @property
    def rho_list(self) -> list[torch.Tensor]:
        return [patch.rho for patch in self.patches]

    @rho_list.setter
    def rho_list(self, rho_list_new) -> None:
        for patch, rho_new in zip(self.patches, rho_list_new):
            patch.rho = rho_new

    def apply_boundaries(self, rho_list: list[torch.Tensor]) -> list[torch.Tensor]:
        """Apply all boundary conditions to `rho` and produce ghost-padded version.
        The list contains the data for each patch."""
        # Create padded version for all patches:
        out_list = []
        for patch, rho in zip(self.patches, rho_list):
            out = torch.zeros(patch.rho_padded_shape, device=rc.device)
            out[Advect.NON_GHOST, Advect.NON_GHOST] = rho
            out_list.append(out)

        # Populate ghost zones across patches where needed:
        for i_patch, (out, adjacency) in enumerate(zip(out_list, self.adjacency)):
            for i_edge, (other_patch, other_edge) in enumerate(adjacency):
                if other_patch < 0:
                    # TODO: handle reflecting boundaries
                    # For now they will be sinks (hence pass)
                    pass
                else:
                    # Pass-through boundary:
                    ghost_data = rho_list[other_patch][Geometry.IN_SLICES[other_edge]]
                    delta_edge = other_edge - i_edge
                    if delta_edge % 2:
                        ghost_data = ghost_data.swapaxes(0, 1)
                    if flip_dims := Geometry.FLIP_DIMS[delta_edge]:
                        ghost_data = ghost_data.flip(dims=flip_dims)
                    out[Geometry.OUT_SLICES[i_edge]] = ghost_data
        return out_list

    def next_rho_list(
        self,
        dt: float,
        rho_list_initial: list[torch.Tensor],
        rho_list_eval: list[torch.Tensor],
    ) -> list[torch.Tensor]:
        """Ingredient of time step: compute rho_initial + dt * f(rho_eval)."""
        return [
            (rho_initial + patch.drho(dt, rho_eval))
            for rho_initial, rho_eval, patch in zip(
                rho_list_initial, self.apply_boundaries(rho_list_eval), self.patches
            )
        ]

    def time_step(self) -> None:
        """Second-order correct time step."""
        rho_list_init = self.rho_list
        rho_list_half = self.next_rho_list(0.5 * self.dt, rho_list_init, rho_list_init)
        self.rho_list = self.next_rho_list(self.dt, rho_list_init, rho_list_half)


def get_displacements(
    vertices: torch.Tensor,
    edges: torch.Tensor,
    quads: torch.Tensor,
    adjacency: torch.Tensor,
    tol: float = 1e-3,
) -> torch.Tensor:
    """Check consistency and collect displacements between adjacent edges."""
    i_quad, i_edge = torch.nonzero(adjacency[..., 0] >= 0).T
    j_quad, j_edge = adjacency[i_quad, i_edge].T
    verts_i = vertices[edges[quads[i_quad, i_edge]]]
    verts_j = vertices[edges[quads[j_quad, j_edge]]].flip(dims=(1,))
    deltas = verts_i - verts_j
    assert torch.all(deltas.std(dim=1) < tol).item()
    displacements = torch.zeros(adjacency.shape, device=rc.device)
    displacements[i_quad, i_edge] = deltas.mean(dim=1)
    return displacements


def equivalence_classes(pairs: torch.Tensor) -> torch.Tensor:
    """Given Npair x 2 array of index pairs that are equivalent,
    compute equivalence class numbers for each original index."""
    # Construct adjacency matrix:
    N = pairs.max() + 1
    i_pair, j_pair = pairs.T
    adjacency_matrix = torch.eye(N, device=rc.device)
    adjacency_matrix[i_pair, j_pair] = 1.0
    adjacency_matrix[j_pair, i_pair] = 1.0

    # Expand to indirect neighbors by repeated multiplication:
    n_non_zero_prev = torch.count_nonzero(adjacency_matrix)
    for i_mult in range(N):
        adjacency_matrix = adjacency_matrix @ adjacency_matrix
        n_non_zero = torch.count_nonzero(adjacency_matrix)
        if n_non_zero == n_non_zero_prev:
            break  # highest-degree connection reached
        n_non_zero_prev = n_non_zero

    # Find first non-zero entry of above (i.e. first equivalent index):
    is_first = torch.logical_and(
        adjacency_matrix.cumsum(dim=1) == adjacency_matrix, adjacency_matrix != 0.0
    )
    first_index = torch.nonzero(is_first)[:, 1]
    assert len(first_index) == N
    return torch.unique(first_index, return_inverse=True)[1]  # minimal class indices
