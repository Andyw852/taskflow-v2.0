# -*- coding: utf-8 -*-
"""Sensitive symmetry audit for thermal-conductivity tensors."""
import numpy as np

ISOTROPIC_SYSTEMS = frozenset({"cubic", "hexagonal", "trigonal", "tetragonal"})


def crystal_system_from_spacegroup(number):
    number = int(number)
    if number <= 2: return "triclinic"
    if number <= 15: return "monoclinic"
    if number <= 74: return "orthorhombic"
    if number <= 142: return "tetragonal"
    if number <= 167: return "trigonal"
    if number <= 194: return "hexagonal"
    return "cubic"


def voigt_to_matrix(voigt):
    xx, yy, zz, yz, xz, xy = [float(x) for x in np.asarray(voigt).ravel()[:6]]
    return np.array([[xx, xy, xz], [xy, yy, yz], [xz, yz, zz]], dtype=float)


def project_in_plane(matrix, normal):
    n = np.asarray(normal, dtype=float)
    n /= np.linalg.norm(n)
    ref = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    e1 = np.cross(n, ref); e1 /= np.linalg.norm(e1)
    e2 = np.cross(n, e1)
    basis = np.column_stack([e1, e2])
    return basis.T @ np.asarray(matrix, dtype=float) @ basis


def audit_kappa_tensor(matrix, crystal_system=None, relative_tolerance=0.02):
    """Audit an in-plane conductivity block.

    The determinant ratio is retained as a rank-collapse diagnostic, but the
    gate uses first-order-sensitive quantities: eigenvalue ratio, diagonal
    mismatch, and off-diagonal magnitude. Isotropy is required only for crystal
    systems where symmetry demands it.
    """
    m = np.asarray(matrix, dtype=float)
    if m.shape != (2, 2):
        raise ValueError("in-plane kappa block must be 2x2")
    m = (m + m.T) / 2.0
    eig = np.linalg.eigvalsh(m)
    scale = max(float(np.trace(m) / 2.0), 1e-12)
    min_abs = max(float(np.min(np.abs(eig))), 1e-12)
    ratio = float(np.max(np.abs(eig)) / min_abs)
    diag = float(abs(m[0, 0] - m[1, 1]) / scale)
    offdiag = float(abs(m[0, 1]) / scale)
    det_ratio = float(np.linalg.det(m) / (scale * scale))
    system = (crystal_system or "unknown").strip().lower()
    threshold = relative_tolerance if system in ISOTROPIC_SYSTEMS else None
    gate = True if threshold is None else bool(
        ratio <= 1.0 + threshold and diag <= threshold and offdiag <= threshold
    )
    return {
        "eigenvalues": [float(x) for x in eig],
        "eigenvalue_ratio": ratio,
        "diagonal_relative_mismatch": diag,
        "offdiag_relative_magnitude": offdiag,
        "determinant_ratio": det_ratio,
        "crystal_system": system,
        "threshold": threshold,
        "gate": gate,
    }


def audit_kappa_voigt(voigt, crystal_system=None, plane_normal=None, relative_tolerance=0.02):
    matrix = voigt_to_matrix(voigt)
    system = (crystal_system or "unknown").strip().lower()
    if plane_normal is not None:
        return audit_kappa_tensor(project_in_plane(matrix, plane_normal), system,
                                  relative_tolerance)
    if system == "cubic":
        eig = np.linalg.eigvalsh((matrix + matrix.T) / 2.0)
        scale = max(float(np.mean(np.diag(matrix))), 1e-12)
        ratio = float(np.max(np.abs(eig)) / max(np.min(np.abs(eig)), 1e-12))
        diag = float((np.max(np.diag(matrix)) - np.min(np.diag(matrix))) / scale)
        offdiag = float(np.max(np.abs(matrix - np.diag(np.diag(matrix)))) / scale)
        return {
            "eigenvalues": [float(x) for x in eig],
            "eigenvalue_ratio": ratio,
            "diagonal_relative_mismatch": diag,
            "offdiag_relative_magnitude": offdiag,
            "crystal_system": system,
            "threshold": relative_tolerance,
            "gate": bool(ratio <= 1.0 + relative_tolerance and
                         diag <= relative_tolerance and
                         offdiag <= relative_tolerance),
        }
    return audit_kappa_tensor(matrix[:2, :2], system, relative_tolerance)
