# This file is part of asyncmd.
#
# asyncmd is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# asyncmd is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with asyncmd. If not, see <https://www.gnu.org/licenses/>.
import os
import abc
import typing
import logging
import numpy as np
import MDAnalysis as mda
from scipy import constants

from .trajectory import Trajectory


logger = logging.getLogger(__name__)


class TrajectoryConcatenator:
    """
    Create concatenated trajectory from given trajectories and frames.

    The concatenate method takes a list of trajectories plus a list of slices
    and returns one trajectory containing only the selected frames in the order
    specified by the slices.
    Velocities are automatically inverted if the step of a slice is negative,
    this can be controlled via the invert_v_for_negative_step attribute.
    We assume that all trajs have the same structure file and attach the
    structure of the first traj if not told otherwise.

    Attributes
    ----------
    invert_v_for_negative_step : bool
        Whether to invert all momenta for segments with negative stride.
    """

    def __init__(self, invert_v_for_negative_step: bool = True):
        """
        Initialize a :class:`TrajectoryConcatenator`.

        Parameters
        ----------
        invert_v_for_negative_step : bool, optional
            Whether to invert all momenta for segments with negative stride,
            by default True.
        """
        self.invert_v_for_negative_step = invert_v_for_negative_step

    def concatenate(self, trajs: "list[Trajectory]", slices: "list[tuple]",
                    tra_out: str, struct_out: typing.Optional[str] = None,
                    overwrite: bool = False,
                    remove_double_frames: bool = True) -> Trajectory:
        """
        Create concatenated trajectory from given trajectories and frames.

        Parameters
        ----------
        trajs : list[Trajectory]
            List of :class:`asyncmd.Trajectory` objects to concatenate.
        slices : list[tuple]
            List of tuples (start, stop, step) specifing the slices of the
            trajectories to take. Must be of len(trajs).
        tra_out : str
            Output trajectory filepath, absolute or relativ to current working
            directory.
        struct_out : str or None, optional
            Output structure filepath, if None we will take the structure file
            of the first trajectory in trajs, by default None.
        overwrite : bool, optional
            Whether we should overwrite existing output trajectories,
            by default False.
        remove_double_frames : bool, optional
            Wheter we should try to remove double frames from the concatenated
            output trajectory.
            Note that we use a simple heuristic to determine double frames,
            we just check if the integration time is the same for both frames,
            by default True

        Returns
        -------
        Trajectory
            The concatenated output trajectory.

        Raises
        ------
        ValueError
            If ``tra_out`` exists and ``overwrite=False``.
        ValueError
            If ``struct_out`` given but the file is not accessible.
        """
        tra_out = os.path.abspath(tra_out)
        if os.path.exists(tra_out) and not overwrite:
            raise ValueError(f"overwrite=False and tra_out exists: {tra_out}")
        struct_out = (trajs[0].structure_file if struct_out is None
                      else os.path.abspath(struct_out))
        if not os.path.isfile(struct_out):
            # although we would expect that it exists if it comes from an
            # existing traj, we still check to catch other unrelated issues :)
            raise ValueError(
                        f"Output structure file must exist ({struct_out})."
                             )

        # special treatment for traj0 because we need n_atoms for the writer
        u0 = mda.Universe(trajs[0].structure_file, *trajs[0].trajectory_files,
                          tpr_resid_from_one=True)
        start0, stop0, step0 = slices[0]
        # if the file exists MDAnalysis will silently overwrite
        with mda.Writer(tra_out, n_atoms=u0.trajectory.n_atoms) as W:
            for ts in u0.trajectory[start0:stop0:step0]:
                if self.invert_v_for_negative_step and step0 < 0:
                    u0.atoms.velocities *= -1
                W.write(u0.atoms)
                if remove_double_frames:
                    # remember the last timestamp, so we can take it out
                    last_time_seen = ts.data["time"]
            del u0  # should free up memory and does no harm?!
            for traj, sl in zip(trajs[1:], slices[1:]):
                u = mda.Universe(traj.structure_file, *traj.trajectory_files,
                                 tpr_resid_from_one=True)
                start, stop, step = sl
                for ts in u.trajectory[start:stop:step]:
                    if remove_double_frames:
                        if last_time_seen == ts.data["time"]:
                            # this is a no-op, as they are they same...
                            # last_time_seen = ts.data["time"]
                            continue  # skip this timestep/go to next iteration
                    if self.invert_v_for_negative_step and step < 0:
                        u.atoms.velocities *= -1
                    W.write(u.atoms)
                    if remove_double_frames:
                        last_time_seen = ts.data["time"]
                del u
        # return (file paths to) the finished trajectory
        return Trajectory(tra_out, struct_out)


class FrameExtractor(abc.ABC):
    """
    Abstract base class for FrameExtractors.

    Implements the `extract` method which is common in all FrameExtractors.
    Subclasses only need to implement `apply_modification` which is called by
    `extract` to modify the frame just before writing it out.
    """

    # extract a single frame with given idx from a trajectory and write it out
    # simplest case is without modification, but useful modifications are e.g.
    # with inverted velocities, with random Maxwell-Boltzmann velocities, etc.

    @abc.abstractmethod
    def apply_modification(self, universe, ts):
        """
        Apply modification to selected frame (timestep/universe).

        This function will be called when the current timestep is at the
        chosen frame index and is expected to apply the subclass specific
        modifications to the frame via modifying the mdanalysis timestep and
        universe objects **inplace**.
        After this function finishes the frame is written out, i.e. with any
        potential modifications applied.
        No return value is expected or considered from this method, the
        modifications of the timestep/universe are nonlocal anyway.

        Parameters
        ----------
        universe : MDAnalysis.core.universe.Universe
            The mdanalysis universe associated with the trajectory.
        ts : MDAnalysis.coordinates.base.Timestep
            The mdanalysis timestep of the frame to extract.
        """
        raise NotImplementedError

    def extract(self, outfile, traj_in: Trajectory, idx: int,
                struct_out=None, overwrite: bool = False) -> Trajectory:
        """
        Extract a single frame from given trajectory and write it out.

        Parameters
        ----------
        outfile : str
            Absolute or relative path to the output trajectory. Expected to be
            with file ending, e.g. "traj.trr".
        traj_in : Trajectory
            Input trajectory from which we will extract the frame at `idx`.
        idx : int
            Index of the frame to extract in `traj_in`.
        struct_out : str, optional
            None, or absolute or relative path to a structure file,
            by default None. If not None we will use the given file as
            structure file for the returned trajectory object, else we use the
            structure file of `traj_in`.
        overwrite : bool, optional
            Whether to overwrite `outfile` if it exists, by default False.

        Returns
        -------
        Trajectory
            Trajectory object holding a trajectory with the extracted frame.

        Raises
        ------
        ValueError
            If `outfile` exists and `overwrite=False`.
        ValueError
            If `struct_out` is given but does not exist.
        """
        # TODO: should we check that idx is an idx, i.e. an int?
        # TODO: make it possible to select a subset of atoms to write out
        #       and also for modification?
        # TODO: should we make it possible to extract multiple frames, i.e.
        #       enable the use of slices (and iterables of indices?)
        outfile = os.path.abspath(outfile)
        if os.path.exists(outfile) and not overwrite:
            raise ValueError(f"overwrite=False but outfile={outfile} exists.")
        struct_out = (traj_in.structure_file if struct_out is None
                      else os.path.abspath(struct_out))
        if not os.path.isfile(struct_out):
            # although we would expect that it exists if it comes from an
            # existing traj, we still check to catch other unrelated issues :)
            raise ValueError("Output structure file must exist."
                             + f"(given struct_out is {struct_out})."
                             )
        u = mda.Universe(traj_in.structure_file, *traj_in.trajectory_files,
                         tpr_resid_from_one=True)
        with mda.Writer(outfile, n_atoms=u.trajectory.n_atoms) as W:
            ts = u.trajectory[idx]
            self.apply_modification(u, ts)
            W.write(u.atoms)
        return Trajectory(trajectory_files=outfile, structure_file=struct_out)


class NoModificationFrameExtractor(FrameExtractor):
    """Extract a frame from a trajectory, write it out without modification."""

    def apply_modification(self, universe, ts):
        """
        Apply no modification to the extracted frame.

        Parameters
        ----------
        universe : MDAnalysis.core.universe.Universe
            The mdanalysis universe associated with the trajectory.
        ts : MDAnalysis.coordinates.base.Timestep
            The mdanalysis timestep of the frame to extract.
        """
        pass


class InvertedVelocitiesFrameExtractor(FrameExtractor):
    """
    Extract a frame from a trajectory, write it out with inverted velocities.
    """

    def apply_modification(self, universe, ts):
        """
        Invert all momenta of the extracted frame.

        Parameters
        ----------
        universe : MDAnalysis.core.universe.Universe
            The mdanalysis universe associated with the trajectory.
        ts : MDAnalysis.coordinates.base.Timestep
            The mdanalysis timestep of the frame to extract.
        """
        ts.velocities *= -1.


class RandomVelocitiesFrameExtractor(FrameExtractor):
    """
    Extract a frame from a trajectory, write it out with randomized velocities.

    Attributes
    ----------
    T : float
        Temperature of the Maxwell-Boltzmann distribution for velocity
        generation, in Kelvin.
    """

    def __init__(self, T: float):
        """
        Initialize a :class:`RandomVelocitiesFrameExtractor`.

        Parameters
        ----------
        T : float
            Temperature of the Maxwell-Boltzmann distribution, in Kelvin.
        """
        self.T = T  # in K
        self._rng = np.random.default_rng()

    def apply_modification(self, universe, ts):
        """
        Draw random Maxwell-Boltzmann velocities for extracted frame.

        Parameters
        ----------
        universe : MDAnalysis.core.universe.Universe
            The mdanalysis universe associated with the trajectory.
        ts : MDAnalysis.coordinates.base.Timestep
            The mdanalysis timestep of the frame to extract.
        """
        # m is in units of g / mol
        # v should be in units of \AA / ps = 100 m / s
        # which means m [10**-3 kg / mol] v**2 [10000 (m/s)**2]
        # is in units of [ 10 kg m**s / (mol * s**2) ]
        # so we use R = N_A * k_B [J / (mol * K) = kg m**2 / (s**2 * mol * K)]
        # and add in a factor 10 to get 1/σ**2 = m / (k_B * T)
        # in the correct units
        scale = np.empty((ts.n_atoms, 3), dtype=np.float64)
        s1d = np.sqrt((self.T * constants.R * 0.1)
                      / universe.atoms.masses
                      )
        # sigma is the same for all 3 cartesian dimensions
        for i in range(3):
            scale[:, i] = s1d
        ts.velocities = self._rng.normal(loc=0, scale=scale)
