import concurrent.futures
from typing import Dict, List, Optional

import h5py
import numpy as np
import torch
import zarr
from tqdm import tqdm

from cleandiffuser.dataset.base_dataset import BaseDataset
from cleandiffuser.dataset.dataset_utils import RotationTransformer, SequenceSampler, dict_apply
from cleandiffuser.dataset.imagecodecs import Jpeg2k, register_codecs
from cleandiffuser.dataset.replay_buffer import ReplayBuffer
from cleandiffuser.utils import MinMaxNormalizer

register_codecs()


class RobomimicDataset(BaseDataset):
    """Robomimic Low-dim imitation learning dataset.

    The dataset chunks the demonstrations into sequences of length `horizon`.
    It uses `MinMaxNormalizer` to normalize the observations and actions to [-1, 1] as default.
    Each batch contains:
    - batch['obs']['state'], low-dim observation of shape (batch_size, horizon, obs_dim)
    - batch['act'], action of shape (batch_size, horizon, act_dim)

    Args:
        dataset_dir (str):
            Path to the dataset directory. Please download from https://diffusion-policy.cs.columbia.edu/data/training/robomimic_lowdim.zip and unzip it.

        horizon (int):
            The length of the sequence.

        pad_before (int):
            The number of steps to pad the beginning of the sequence.

        pad_after (int):
            The number of steps to pad the end of the sequence.

        obs_keys (List[str]):
            The observation keys in the hdf5 file.

        abs_action (bool):
            Whether to use absolute action.

        rotation_rep (str):
            The representation of the rotation.

    Examples:
        >>> dataset = RobomimicDataset(dataset_dir='dev/robomimic_lowdim', horizon=4)
        >>> dataloader = DataLoader(dataset, batch_size=32, shuffle=True)
        >>> batch = next(iter(dataloader))
        >>> obs = batch["obs"]["state"]
        >>> act = batch["act"]

        >>> normalizer = dataset.get_normalizer()
        >>> obs = env.reset()[None, :]
        >>> obs = normalizer["obs"]["state"].unnormalize(obs)
        >>> act = behavior_clone_policy(obs)
        >>> act = normalizer["act"].unnormalize(act)
        >>> obs, rew, done, info = env.step(act)
    """

    def __init__(
        self,
        dataset_dir: str,
        horizon: int = 1,
        pad_before: int = 0,
        pad_after: int = 0,
        obs_keys: List[str] = ("object", "robot0_eef_pos", "robot0_eef_quat", "robot0_gripper_qpos"),
        abs_action: bool = False,
        rotation_rep: str = "rotation_6d",
    ):
        super().__init__()
        self.rotation_transformer = RotationTransformer(from_rep="axis_angle", to_rep=rotation_rep)

        self.replay_buffer = ReplayBuffer.create_empty_numpy()
        with h5py.File(dataset_dir) as file:
            demos = file["data"]
            for i in tqdm(range(len(demos)), desc="Loading hdf5 to ReplayBuffer"):
                demo = demos[f"demo_{i}"]
                episode = _data_to_obs(
                    raw_obs=demo["obs"],
                    raw_actions=demo["actions"][:].astype(np.float32),
                    obs_keys=obs_keys,
                    abs_action=abs_action,
                    rotation_transformer=self.rotation_transformer,
                )
                self.replay_buffer.add_episode(episode)

        self.sampler = SequenceSampler(
            replay_buffer=self.replay_buffer, sequence_length=horizon, pad_before=pad_before, pad_after=pad_after
        )

        self.state_normalizer = MinMaxNormalizer(self.replay_buffer["obs"][:], -1)
        self.action_normalizer = MinMaxNormalizer(self.replay_buffer["action"][:], -1)

        self.horizon = horizon
        self.pad_before = pad_before
        self.pad_after = pad_after
        self.abs_action = abs_action
        self.normalizer = self.get_normalizer()

    def undo_transform_action(self, action):
        raw_shape = action.shape
        if raw_shape[-1] == 20:
            # dual arm
            action = action.reshape(-1, 2, 10)

        d_rot = action.shape[-1] - 4
        pos = action[..., :3]
        rot = action[..., 3 : 3 + d_rot]
        gripper = action[..., [-1]]
        rot = self.rotation_transformer.inverse(rot)
        uaction = np.concatenate([pos, rot, gripper], axis=-1)

        if raw_shape[-1] == 20:
            # dual arm
            uaction = uaction.reshape(*raw_shape[:-1], 14)

        return uaction

    def get_normalizer(self):
        return {
            "obs": {
                "state": self.state_normalizer,
            },
            "act": self.action_normalizer,
        }

    def sample_to_data(self, sample):
        state = sample["obs"].astype(np.float32)
        state = self.normalizer["obs"]["state"].normalize(state)

        action = sample["action"].astype(np.float32)
        action = self.normalizer["action"].normalize(action)
        data = {
            "obs": {"state": state},
            "action": action,
        }
        return data

    def __str__(self) -> str:
        return f"Keys: {self.replay_buffer.keys()} Steps: {self.replay_buffer.n_steps} Episodes: {self.replay_buffer.n_episodes}"

    def __len__(self) -> int:
        return len(self.sampler)

    def __getitem__(self, idx: int):
        sample = self.sampler.sample_sequence(idx)

        state = self.normalizer["obs"]["state"].normalize(sample["obs"])
        action = self.normalizer["action"].normalize(sample["action"])

        return {"obs": {"state": state}, "act": action}


def _data_to_obs(raw_obs, raw_actions, obs_keys, abs_action, rotation_transformer):
    obs = np.concatenate([raw_obs[key] for key in obs_keys], axis=-1).astype(np.float32)

    if abs_action:
        is_dual_arm = False
        if raw_actions.shape[-1] == 14:
            # dual arm
            raw_actions = raw_actions.reshape(-1, 2, 7)
            is_dual_arm = True

        pos = raw_actions[..., :3]
        rot = raw_actions[..., 3:6]
        gripper = raw_actions[..., 6:]
        rot = rotation_transformer.forward(rot)
        raw_actions = np.concatenate([pos, rot, gripper], axis=-1).astype(np.float32)

        if is_dual_arm:
            raw_actions = raw_actions.reshape(-1, 20)

    data = {"obs": obs, "action": raw_actions}
    return data


class RobomimicImageDataset(BaseDataset):
    def __init__(
        self,
        dataset_dir,
        shape_meta: Optional[dict] = None,
        n_obs_steps: Optional[int] = None,
        horizon: int = 1,
        pad_before: int = 0,
        pad_after: int = 0,
        abs_action: bool = False,
        rotation_rep: str = "rotation_6d",
    ):
        super().__init__()

        if shape_meta is None:
            shape_meta = {
                "obs": {
                    "agentview_image": {"shape": [3, 84, 84], "type": "rgb"},
                    "robot0_eye_in_hand_image": {"shape": [3, 84, 84], "type": "rgb"},
                    "robot0_eef_pos": {
                        "shape": [3],
                        "type": "low_dim",
                    },
                    "robot0_eef_quat": {
                        "shape": [4],
                        "type": "low_dim",
                    },
                    "robot0_gripper_qpos": {
                        "shape": [2],
                        "type": "low_dim",
                    },
                },
                "action": {"shape": [7 if not abs_action else 10]},
            }

        self.rotation_transformer = RotationTransformer(from_rep="axis_angle", to_rep=rotation_rep)

        self.replay_buffer = _convert_robomimic_to_replay(
            store=zarr.MemoryStore(),
            shape_meta=shape_meta,
            dataset_path=dataset_dir,
            abs_action=abs_action,
            rotation_transformer=self.rotation_transformer,
        )

        rgb_keys = list()
        lowdim_keys = list()
        obs_shape_meta = shape_meta["obs"]
        for key, attr in obs_shape_meta.items():
            type = attr.get("type", "low_dim")
            if type == "rgb":
                rgb_keys.append(key)
            elif type == "low_dim":
                lowdim_keys.append(key)

        key_first_k = dict()
        if n_obs_steps is not None:
            # only take first k obs from images
            for key in rgb_keys + lowdim_keys:
                key_first_k[key] = n_obs_steps

        self.sampler = SequenceSampler(
            replay_buffer=self.replay_buffer,
            sequence_length=horizon,
            pad_before=pad_before,
            pad_after=pad_after,
            key_first_k=key_first_k,
        )

        self.shape_meta = shape_meta
        self.rgb_keys = rgb_keys
        self.lowdim_keys = lowdim_keys
        self.abs_action = abs_action
        self.horizon = horizon
        self.pad_before = pad_before
        self.pad_after = pad_after
        self.n_obs_steps = n_obs_steps

        self.normalizer = {"obs": {}, "action": None}
        for key in self.lowdim_keys:
            self.normalizer["obs"][key] = MinMaxNormalizer(self.replay_buffer[key][:])
        for key in self.rgb_keys:
            max_rgb_values = np.full(shape_meta["obs"][key]["shape"], fill_value=255, dtype=np.float32)
            min_rgb_values = np.zeros(shape_meta["obs"][key]["shape"], dtype=np.float32)
            self.normalizer["obs"][key] = MinMaxNormalizer(None, -3, max_rgb_values, min_rgb_values)
        self.normalizer["action"] = MinMaxNormalizer(self.replay_buffer["action"][:])

    def get_normalizer(self):
        return self.normalizer

    def __str__(self) -> str:
        return f"Keys: {self.replay_buffer.keys()} Steps: {self.replay_buffer.n_steps} Episodes: {self.replay_buffer.n_episodes}"

    def __len__(self) -> int:
        return len(self.sampler)

    def __getitem__(self, idx: int) -> Dict[str, torch.Tensor]:
        sample = self.sampler.sample_sequence(idx)

        # obs
        # to save RAM, only return first n_obs_steps of OBS
        # since the rest will be discarded anyway.
        # when self.n_obs_steps is None
        # this slice does nothing (takes all)
        T_slice = slice(self.n_obs_steps)

        obs_dict = dict()
        for key in self.rgb_keys:
            # move channel last to channel first
            # T,H,W,C
            # convert uint8 image to float32
            obs_dict[key] = np.moveaxis(sample[key][T_slice], -1, 1).astype(np.float32)
            # T,C,H,W
            del sample[key]
            obs_dict[key] = self.normalizer["obs"][key].normalize(obs_dict[key])

        for key in self.lowdim_keys:
            obs_dict[key] = sample[key][T_slice].astype(np.float32)
            del sample[key]
            obs_dict[key] = self.normalizer["obs"][key].normalize(obs_dict[key])

        # action
        action = sample["action"].astype(np.float32)
        action = self.normalizer["action"].normalize(action)

        torch_data = {"obs": dict_apply(obs_dict, torch.tensor), "action": torch.tensor(action)}
        return torch_data

    def undo_transform_action(self, action):
        raw_shape = action.shape
        if raw_shape[-1] == 20:
            # dual arm
            action = action.reshape(-1, 2, 10)

        d_rot = action.shape[-1] - 4
        pos = action[..., :3]
        rot = action[..., 3 : 3 + d_rot]
        gripper = action[..., [-1]]
        rot = self.rotation_transformer.inverse(rot)
        uaction = np.concatenate([pos, rot, gripper], axis=-1)

        if raw_shape[-1] == 20:
            # dual arm
            uaction = uaction.reshape(*raw_shape[:-1], 14)

        return uaction


def _convert_actions(raw_actions, abs_action, rotation_transformer):
    actions = raw_actions
    if abs_action:
        is_dual_arm = False
        if raw_actions.shape[-1] == 14:
            # dual arm
            raw_actions = raw_actions.reshape(-1, 2, 7)
            is_dual_arm = True

        pos = raw_actions[..., :3]
        rot = raw_actions[..., 3:6]
        gripper = raw_actions[..., 6:]
        rot = rotation_transformer.forward(rot)
        raw_actions = np.concatenate([pos, rot, gripper], axis=-1).astype(np.float32)

        if is_dual_arm:
            raw_actions = raw_actions.reshape(-1, 20)
        actions = raw_actions
    return actions


def _convert_robomimic_to_replay(
    store: zarr.MemoryStore,
    shape_meta: Dict[str, dict],
    dataset_path: str,
    abs_action: bool,
    rotation_transformer: RotationTransformer,
    n_workers: Optional[int] = None,
    max_inflight_tasks: Optional[int] = None,
):
    """Convert Robomimic dataset to ReplayBuffer

    A ReplayBuffer is a `zarr.Group` or Dict[str, dict] that contains the following keys:
    - data: zarr.Group or Dict[str, dict]
        Contains the data. All data should be stored as numpy arrays with the same length.
    - meta: zarr.Group or Dict[str, dict]
        Contains key "episode_ends", which is a numpy array of shape (n_episodes,) that contains the
        end index of each episode in the data.

    Args:
        store (zarr.Store):
            zarr.MemoryStore()
        shape_meta (dict):
            Shape metadata of the dataset. Defaults to:
            {
                "obs": {
                    "agentview_image": {
                        "shape": [3, 84, 84], "type": "rgb"},
                    "robot0_eye_in_hand_image": {
                        "shape": [3, 84, 84], "type": "rgb"},
                    "robot0_eef_pos": {
                        "shape": [3, ], "type": "low_dim"},
                    "robot0_eef_quat": {
                        "shape": [4, ], "type": "low_dim"},
                    "robot0_gripper_qpos": {
                        "shape": [2, ], "type": "low_dim"},
                }
                "act": {"shape": [7, ]},  # 10 for `abs_action`
            }

        dataset_path (str):
            Path to the Robomimic dataset
        abs_action (bool):
            Whether to use position or velocity control
        rotation_transformer (RotationTransformer):
            Rotation transformer to convert rotation representation
        n_workers (Optional[int]):
            Number of workers. Defaults to None
        max_inflight_tasks (Optional[int]):
            Maximum number of inflight tasks. Defaults to None

    """
    import multiprocessing

    if n_workers is None:
        n_workers = multiprocessing.cpu_count()
    if max_inflight_tasks is None:
        max_inflight_tasks = n_workers * 5

    # parse shape_meta
    rgb_keys = list()
    lowdim_keys = list()
    # construct compressors and chunks
    obs_shape_meta = shape_meta["obs"]
    for key, attr in obs_shape_meta.items():
        shape = attr["shape"]
        type = attr.get("type", "low_dim")
        if type == "rgb":
            rgb_keys.append(key)
        elif type == "low_dim":
            lowdim_keys.append(key)

    # create zarr group
    root = zarr.group(store)
    data_group = root.require_group("data", overwrite=True)
    meta_group = root.require_group("meta", overwrite=True)

    with h5py.File(dataset_path) as file:
        # count total steps
        demos = file["data"]
        episode_ends = list()
        prev_end = 0
        for i in range(len(demos)):
            demo = demos[f"demo_{i}"]
            episode_length = demo["actions"].shape[0]
            episode_end = prev_end + episode_length
            prev_end = episode_end
            episode_ends.append(episode_end)
        n_steps = episode_ends[-1]
        episode_starts = [0] + episode_ends[:-1]
        _ = meta_group.array("episode_ends", episode_ends, dtype=np.int64, compressor=None, overwrite=True)

        # save lowdim data
        for key in tqdm(lowdim_keys + ["action"], desc="Loading lowdim data"):
            data_key = "obs/" + key
            if key == "action":
                data_key = "actions"
            this_data = list()
            for i in range(len(demos)):
                demo = demos[f"demo_{i}"]
                this_data.append(demo[data_key][:].astype(np.float32))
            this_data = np.concatenate(this_data, axis=0)
            if key == "action":
                this_data = _convert_actions(
                    raw_actions=this_data, abs_action=abs_action, rotation_transformer=rotation_transformer
                )
                assert this_data.shape == (n_steps,) + tuple(
                    shape_meta["action"]["shape"]
                ), f"{this_data.shape} != {(n_steps,) + tuple(shape_meta['action']['shape'])}"
            else:
                assert this_data.shape == (n_steps,) + tuple(shape_meta["obs"][key]["shape"])
            _ = data_group.array(
                name=key,
                data=this_data,
                shape=this_data.shape,
                chunks=this_data.shape,
                compressor=None,
                dtype=this_data.dtype,
            )

        def img_copy(zarr_arr, zarr_idx, hdf5_arr, hdf5_idx):
            try:
                zarr_arr[zarr_idx] = hdf5_arr[hdf5_idx]
                # make sure we can successfully decode
                _ = zarr_arr[zarr_idx]
                return True
            except Exception:
                return False

        with tqdm(total=n_steps * len(rgb_keys), desc="Loading image data", mininterval=1.0) as pbar:
            # one chunk per thread, therefore no synchronization needed
            with concurrent.futures.ThreadPoolExecutor(max_workers=n_workers) as executor:
                futures = set()
                for key in rgb_keys:
                    data_key = "obs/" + key
                    shape = tuple(shape_meta["obs"][key]["shape"])
                    c, h, w = shape
                    this_compressor = Jpeg2k(level=50)
                    img_arr = data_group.require_dataset(
                        name=key,
                        shape=(n_steps, h, w, c),
                        chunks=(1, h, w, c),
                        compressor=this_compressor,
                        dtype=np.uint8,
                    )
                    for episode_idx in range(len(demos)):
                        demo = demos[f"demo_{episode_idx}"]
                        hdf5_arr = demo["obs"][key]
                        for hdf5_idx in range(hdf5_arr.shape[0]):
                            if len(futures) >= max_inflight_tasks:
                                # limit number of inflight tasks
                                completed, futures = concurrent.futures.wait(
                                    futures, return_when=concurrent.futures.FIRST_COMPLETED
                                )
                                for f in completed:
                                    if not f.result():
                                        raise RuntimeError("Failed to encode image!")
                                pbar.update(len(completed))

                            zarr_idx = episode_starts[episode_idx] + hdf5_idx
                            futures.add(executor.submit(img_copy, img_arr, zarr_idx, hdf5_arr, hdf5_idx))
                completed, futures = concurrent.futures.wait(futures)
                for f in completed:
                    if not f.result():
                        raise RuntimeError("Failed to encode image!")
                pbar.update(len(completed))

    replay_buffer = ReplayBuffer(root)
    return replay_buffer
