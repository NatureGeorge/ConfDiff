"""SE(3) diffusion methods."""
"""
----------------
MIT License

Copyright (c) 2022 Jason Yim, Brian L Trippe, Valentin De Bortoli, Emile Mathieu

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
----------------
This file may have been modified by Bytedance Ltd. and/or its affiliates (“Bytedance's Modifications”). 
All Bytedance's Modifications are Copyright (2024) Bytedance Ltd. and/or its affiliates. 
"""
import numpy as np
from . import so3_diffuser
from . import r3_diffuser
from scipy.spatial.transform import Rotation
from openfold.utils import rigid_utils as ru

import torch
import logging


def _extract_trans_rots(rigid: ru.Rigid):
    rot = rigid.get_rots().get_rot_mats().cpu().numpy()
    rot_shape = rot.shape
    num_rots = np.cumprod(rot_shape[:-2])[-1]
    rot = rot.reshape((num_rots, 3, 3))
    rot = Rotation.from_matrix(rot).as_rotvec().reshape(rot_shape[:-2] + (3,))
    tran = rigid.get_trans().cpu().numpy()
    return tran, rot


def _assemble_rigid(rotvec, trans, device=None):
    rotvec_shape = rotvec.shape
    num_rotvecs = np.cumprod(rotvec_shape[:-1])[-1]
    rotvec = rotvec.reshape((num_rotvecs, 3))
    rotmat = (
        Rotation.from_rotvec(rotvec).as_matrix().reshape(rotvec_shape[:-1] + (3, 3))
    )
    return ru.Rigid(
        rots=ru.Rotation(rot_mats=torch.tensor(rotmat, device=device)),
        trans=torch.tensor(trans, device=device),
    )


def quat_to_rotvec(quat, eps=1e-6):
    # w > 0 to ensure 0 <= angle <= pi
    flip = (quat[..., :1] < 0).float()
    quat = (-1 * quat) * flip + (1 - flip) * quat

    angle = 2 * torch.atan2(torch.linalg.norm(quat[..., 1:], dim=-1), quat[..., 0])

    angle2 = angle * angle
    small_angle_scales = 2 + angle2 / 12 + 7 * angle2 * angle2 / 2880
    large_angle_scales = angle / torch.sin(angle / 2 + eps)

    small_angles = (angle <= 1e-3).float()
    rot_vec_scale = (
        small_angle_scales * small_angles + (1 - small_angles) * large_angle_scales
    )
    rot_vec = rot_vec_scale[..., None] * quat[..., 1:]
    return rot_vec


class SE3Diffuser:

    def __init__(self, se3_conf):
        self._log = logging.getLogger(__name__)
        self._se3_conf = se3_conf

        self._diffuse_rot = se3_conf.diffuse_rot
        self._so3_diffuser = so3_diffuser.SO3Diffuser(self._se3_conf.so3)

        self._diffuse_trans = se3_conf.diffuse_trans
        self._r3_diffuser = r3_diffuser.R3Diffuser(self._se3_conf.r3)

    def forward_marginal(
        self,
        rigids_0: ru.Rigid,
        t: float,
        diffuse_mask: np.ndarray = None,
        as_tensor_7: bool = True,
    ):
        """
        Args:
            rigids_0: [..., N] openfold Rigid objects
            t: continuous time in [0, 1].

        Returns:
            rigids_t: [..., N] noised rigid. [..., N, 7] if as_tensor_7 is true.
            trans_score: [..., N, 3] translation score
            rot_score: [..., N, 3] rotation score
            trans_score_norm: [...] translation score norm
            rot_score_norm: [...] rotation score norm
        """
        trans_0, rot_0 = _extract_trans_rots(rigids_0)

        if not self._diffuse_rot:
            rot_t, rot_score, rot_score_scaling = (
                rot_0,
                np.zeros_like(rot_0),
                np.ones_like(t),
            )
        else:
            rot_t, rot_score = self._so3_diffuser.forward_marginal(rot_0, t)
            rot_score_scaling = self._so3_diffuser.score_scaling(t)

        if not self._diffuse_trans:
            trans_t, trans_score, trans_score_scaling = (
                trans_0,
                np.zeros_like(trans_0),
                np.ones_like(t),
            )
        else:
            trans_t, trans_score = self._r3_diffuser.forward_marginal(trans_0, t)
            trans_score_scaling = self._r3_diffuser.score_scaling(t)

        if diffuse_mask is not None:
            rot_t = self._apply_mask(rot_t, rot_0, diffuse_mask[..., None])
            trans_t = self._apply_mask(trans_t, trans_0, diffuse_mask[..., None])

            trans_score = self._apply_mask(
                trans_score, np.zeros_like(trans_score), diffuse_mask[..., None]
            )
            rot_score = self._apply_mask(
                rot_score, np.zeros_like(rot_score), diffuse_mask[..., None]
            )
        rigids_t = _assemble_rigid(rot_t, trans_t)
        if as_tensor_7:
            rigids_t = rigids_t.to_tensor_7()
        return {
            "rigids_t": rigids_t,
            "trans_score": trans_score,
            "rot_score": rot_score,
            "trans_score_scaling": trans_score_scaling,
            "rot_score_scaling": rot_score_scaling,
        }

    def calc_trans_0(self, trans_score, trans_t, t):
        return self._r3_diffuser.calc_trans_0(trans_score, trans_t, t)

    def calc_trans_score(self, trans_t, trans_0, t, use_torch=False, scale=True):
        return self._r3_diffuser.score(
            trans_t, trans_0, t, use_torch=use_torch, scale=scale
        )

    def calc_rot_score(self, rots_t, rots_0, t):
        rots_0_inv = rots_0.invert()
        quats_0_inv = rots_0_inv.get_quats()
        quats_t = rots_t.get_quats()
        quats_0t = ru.quat_multiply(quats_0_inv, quats_t)
        rotvec_0t = quat_to_rotvec(quats_0t)
        return self._so3_diffuser.torch_score(rotvec_0t, t)

    def _apply_mask(self, x_diff, x_fixed, diff_mask):
        return diff_mask * x_diff + (1 - diff_mask) * x_fixed

    def trans_parameters(self, trans_t, score_t, t, dt, mask):
        return self._r3_diffuser.distribution(trans_t, score_t, t, dt, mask)

    def score(
        self,
        rigid_0: ru.Rigid,
        rigid_t: ru.Rigid,
        t: float,
    ):
        tran_0, rot_0 = _extract_trans_rots(rigid_0)
        tran_t, rot_t = _extract_trans_rots(rigid_t)

        if not self._diffuse_rot:
            rot_score = np.zeros_like(rot_0)
        else:
            rot_score = self._so3_diffuser.score(rot_t, t)

        if not self._diffuse_trans:
            trans_score = np.zeros_like(tran_0)
        else:
            trans_score = self._r3_diffuser.score(tran_t, tran_0, t)

        return trans_score, rot_score

    def score_scaling(self, t):
        rot_score_scaling = self._so3_diffuser.score_scaling(t)
        trans_score_scaling = self._r3_diffuser.score_scaling(t)
        return rot_score_scaling, trans_score_scaling

    def reverse(
        self,
        rigids_t: ru.Rigid,
        rot_score: np.ndarray,
        trans_score: np.ndarray,
        t: float,
        dt: float,
        diffuse_mask: np.ndarray = None,
        center: bool = True,
        noise_scale: float = 1.0,
    ):
        """Reverse sampling function from (t) to (t-1).

        Args:
            rigid_t: [..., N] protein rigid objects at time t.
            rot_score: [..., N, 3] rotation score.
            trans_score: [..., N, 3] translation score.
            t: continuous time in [0, 1].
            dt: continuous step size in [0, 1].
            mask: [..., N] which residues to update.
            center: true to set center of mass to zero after step

        Returns:
            rigid_t_1: [..., N] protein rigid objects at time t-1.
        """
        trans_t, rot_t = _extract_trans_rots(rigids_t)
        if not self._diffuse_rot:
            rot_t_1 = rot_t
        else:
            rot_t_1 = self._so3_diffuser.reverse(
                rot_t=rot_t,
                score_t=rot_score,
                t=t,
                dt=dt,
                noise_scale=noise_scale,
            )
        if not self._diffuse_trans:
            trans_t_1 = trans_t
        else:
            trans_t_1 = self._r3_diffuser.reverse(
                x_t=trans_t,
                score_t=trans_score,
                t=t,
                dt=dt,
                center=center,
                noise_scale=noise_scale,
            )

        if diffuse_mask is not None:
            if not isinstance(diffuse_mask, np.ndarray):
                diffuse_mask = diffuse_mask.cpu().numpy()
            trans_t_1 = self._apply_mask(trans_t_1, trans_t, diffuse_mask[..., None])
            rot_t_1 = self._apply_mask(rot_t_1, rot_t, diffuse_mask[..., None])

        return _assemble_rigid(rot_t_1, trans_t_1, device=rigids_t.device)

    def sample_ref(
        self,
        n_samples: int,
        seq_len=None,
        impute: ru.Rigid = None,
        diffuse_mask: np.ndarray = None,
        as_tensor_7: bool = False,
        device=None,
    ):
        """Samples rigids from reference distribution.

        Args:
            n_samples: Number of samples.
            impute: Rigid objects to use as imputation values if either
                translations or rotations are not diffused.
        """
        if impute is not None:
            assert impute.shape[0] == n_samples
            trans_impute, rot_impute = _extract_trans_rots(impute)
            trans_impute = trans_impute.reshape((n_samples, 3))
            rot_impute = rot_impute.reshape((n_samples, 3))
            trans_impute = self._r3_diffuser._scale(trans_impute)

        if diffuse_mask is not None and impute is None:
            raise ValueError("Must provide imputation values.")

        if (not self._diffuse_rot) and impute is None:
            raise ValueError("Must provide imputation values.")

        if (not self._diffuse_trans) and impute is None:
            raise ValueError("Must provide imputation values.")

        if self._diffuse_rot:
            rot_ref = self._so3_diffuser.sample_ref(
                n_samples=n_samples, seq_len=seq_len
            )
        else:
            rot_ref = rot_impute

        if self._diffuse_trans:
            trans_ref = self._r3_diffuser.sample_ref(
                n_samples=n_samples, seq_len=seq_len
            )
        else:
            trans_ref = trans_impute

        if diffuse_mask is not None:
            rot_ref = self._apply_mask(rot_ref, rot_impute, diffuse_mask[..., None])
            trans_ref = self._apply_mask(
                trans_ref, trans_impute, diffuse_mask[..., None]
            )
        trans_ref = self._r3_diffuser._unscale(trans_ref)
        rigids_t = _assemble_rigid(rot_ref, trans_ref, device=device)
        if as_tensor_7:
            rigids_t = rigids_t.to_tensor_7()
        return rigids_t
