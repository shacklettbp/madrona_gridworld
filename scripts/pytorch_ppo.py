from madrona_escape_room_learn import (
        train, profile, TrainConfig, PPOConfig, SimInterface,
        ActorCritic, DiscreteActor, Critic, 
        BackboneShared, BackboneSeparate,
        BackboneEncoder, RecurrentBackboneEncoder,
    )
from madrona_escape_room_learn.models import (
        MLP, LinearLayerDiscreteActor, LinearLayerCritic,
    )

from madrona_escape_room_learn.rnn import LSTM

import argparse
import pathlib
import pickle as pkl
from gridworld import GridWorld
from tabular_policy import TabularPolicy, TabularValue
import torch
import warnings
#warnings.filterwarnings("error")
import matplotlib.pyplot as plt
import time
from torch.utils.tensorboard import SummaryWriter

class PPOTabularActor(DiscreteActor):
    def __init__(self, num_states, num_actions):
        tbl = TabularPolicy(num_states, num_actions, False)
        eval_policy = lambda states: tbl.policy[states.squeeze(-1)]
        super().__init__([num_actions], eval_policy)
        self.tbl = tbl


class PPOTabularCritic(Critic):
    def __init__(self, num_states):
        tbl = TabularValue(num_states)
        eval_V = lambda states: tbl.V[states]
        super().__init__(eval_V)
        self.tbl = tbl

arg_parser = argparse.ArgumentParser()
arg_parser.add_argument('--num-worlds', type=int, required=True)
arg_parser.add_argument('--num-updates', type=int, required=True)
arg_parser.add_argument('--lr', type=float, default=0.01)
arg_parser.add_argument('--gamma', type=float, default=0.998)
arg_parser.add_argument('--steps-per-update', type=int, default=4)
arg_parser.add_argument('--gpu-id', type=int, default=0)
arg_parser.add_argument('--entropy-loss-coef', type=float, default=0.3)
arg_parser.add_argument('--value-loss-coef', type=float, default=0.5)
arg_parser.add_argument('--cpu-sim', action='store_true')
arg_parser.add_argument('--fp16', action='store_true')
arg_parser.add_argument('--plot', action='store_true')
arg_parser.add_argument('--dnn', action='store_true')
arg_parser.add_argument('--num-channels', type=int, default=1024)
arg_parser.add_argument('--separate-value', action='store_true')
arg_parser.add_argument('--actor-rnn', action='store_true')
arg_parser.add_argument('--critic-rnn', action='store_true')
arg_parser.add_argument('--num-bptt-chunks', type=int, default=1)
arg_parser.add_argument('--profile-report', action='store_true')
arg_parser.add_argument('--seed', type=int, default=5)
arg_parser.add_argument('--tag', type=str, default=None)
# Working DNN hyperparams:
# --num-worlds 1024 --num-updates 1000 --dnn --lr 0.001 --entropy-loss-coef 0.1
# --num-worlds 1024 --num-updates 1000 --dnn --lr 0.001 --entropy-loss-coef 0.3 --separate-value
# Alternatives (fast):
# --num-worlds 1024 --num-updates 1000 --dnn --lr 0.001 --entropy-loss-coef 0.3 --steps-per-update 10 --separate-value --num-channels 64 --gamma 0.9 
# --num-worlds 1024 --num-updates 1000 --dnn --lr 0.001 --entropy-loss-coef 0.3 --steps-per-update 10 --separate-value --num-channels 256 --gamma 0.998

args = arg_parser.parse_args()
torch.manual_seed(args.seed)

with open(pathlib.Path(__file__).parent / "world_configs/test_world.pkl", 'rb') as handle:
    start_cell, end_cell, rewards, walls = pkl.load(handle)

print(start_cell)

world = GridWorld(args.num_worlds, start_cell, end_cell, rewards, walls)

if torch.cuda.is_available():
    dev = torch.device(f'cuda:{args.gpu_id}')
elif torch.backends.mps.is_available() and False:
    dev = torch.device('mps')
else:
    dev = torch.device('cpu')

num_rows = walls.shape[0]
num_cols = walls.shape[1]

run_name = f"ppogrid__{args.num_worlds}__{args.steps_per_update}__{args.seed}__{int(time.time())}_torch"

num_states = num_rows * num_cols
num_actions = 4 

visit_dict = torch.zeros((walls.shape[0], walls.shape[1], 4), dtype=int, device=dev) # Key: [obs, action], Value: # visits
visit_dict[start_cell[0], start_cell[1], :] = 1

start_time = time.time()

def to1D(obs):
    with torch.no_grad():
        obs_1d = obs[:, 0] * num_cols + obs[:, 1]
        return obs_1d.view(*obs.shape[:-1], 1)

class LearningCallback:
    def __init__(self, profile_report):
        self.mean_fps = 0
        self.profile_report = profile_report

    def __call__(self, update_idx, update_time, update_results, learning_state):
        update_id = update_idx + 1
        fps = args.num_worlds * args.steps_per_update / update_time
        self.mean_fps += (fps - self.mean_fps) / update_id

        if update_id != 1 and update_id % 10 != 0:
            return

        ppo = update_results.ppo_stats

        with torch.no_grad():
            reward_mean = update_results.rewards.mean().cpu().item()
            reward_min = update_results.rewards.min().cpu().item()
            reward_max = update_results.rewards.max().cpu().item()

            value_mean = update_results.values.mean().cpu().item()
            value_min = update_results.values.min().cpu().item()
            value_max = update_results.values.max().cpu().item()

            advantage_mean = update_results.advantages.mean().cpu().item()
            advantage_min = update_results.advantages.min().cpu().item()
            advantage_max = update_results.advantages.max().cpu().item()

            bootstrap_value_mean = update_results.bootstrap_values.mean().cpu().item()
            bootstrap_value_min = update_results.bootstrap_values.min().cpu().item()
            bootstrap_value_max = update_results.bootstrap_values.max().cpu().item()

            vnorm_mu = learning_state.value_normalizer.mu.cpu().item()
            vnorm_sigma = learning_state.value_normalizer.sigma.cpu().item()

        print(f"\nUpdate: {update_id}")
        print(f"    Loss: {ppo.loss: .3e}, A: {ppo.action_loss: .3e}, V: {ppo.value_loss: .3e}, E: {ppo.entropy_loss: .3e}")
        print()
        print(f"    Rewards          => Avg: {reward_mean: .3e}, Min: {reward_min: .3e}, Max: {reward_max: .3e}")
        print(f"    Values           => Avg: {value_mean: .3e}, Min: {value_min: .3e}, Max: {value_max: .3e}")
        print(f"    Advantages       => Avg: {advantage_mean: .3e}, Min: {advantage_min: .3e}, Max: {advantage_max: .3e}")
        print(f"    Bootstrap Values => Avg: {bootstrap_value_mean: .3e}, Min: {bootstrap_value_min: .3e}, Max: {bootstrap_value_max: .3e}")
        print(f"    Returns          => Avg: {ppo.returns_mean}, σ: {ppo.returns_stddev}")
        print(f"    Value Normalizer => Mean: {vnorm_mu: .3e}, σ: {vnorm_sigma :.3e}")

        if self.profile_report:
            print()
            print(f"    FPS: {fps:.0f}, Update Time: {update_time:.2f}, Avg FPS: {self.mean_fps:.0f}")
            print(f"    PyTorch Memory Usage: {torch.cuda.memory_reserved() / 1024 / 1024 / 1024:.3f}GB (Reserved), {torch.cuda.max_memory_allocated() / 1024 / 1024 / 1024:.3f}GB (Current)")
            profile.report()



update_cb = LearningCallback(args.profile_report)

if args.dnn:
    def process_obs(obs):
        div = torch.tensor([[1 / float(num_rows), 1 / float(num_cols)]],
            dtype=torch.float32, device=obs.device)
        return obs.float() * div

    def make_rnn_encoder(num_channels):
        return RecurrentBackboneEncoder(
            net = MLP(
                input_dim = 2,
                num_channels = num_channels // 2,
                num_layers = 2,
            ),
            rnn = LSTM(
                in_channels = num_channels // 2,
                hidden_channels = num_channels,
                num_layers = 1,
            )
        )

    def make_normal_encoder(num_channels):
        return BackboneEncoder(MLP(
            input_dim = 2,
            num_channels = num_channels,
            num_layers = 2,
        ))

    if args.separate_value:
        # Use different channel dims just to make sure everything is being passed correctly
        backbone = BackboneSeparate(
            process_obs = process_obs,
            actor_encoder = make_rnn_encoder(args.num_channels) if args.actor_rnn else make_normal_encoder(args.num_channels),
            critic_encoder = make_rnn_encoder(args.num_channels // 2) if args.critic_rnn else make_normal_encoder(args.num_channels // 2),
        )

        actor_input = args.num_channels 
        critic_input = args.num_channels // 2
    else:
        assert(args.actor_rnn == args.critic_rnn)

        backbone = BackboneShared(
            process_obs = process_obs,
            encoder = make_rnn_encoder(args.num_channels) if args.actor_rnn else make_normal_encoder(args.num_channels),
        )

        actor_input = args.num_channels
        critic_input = args.num_channels

    policy = ActorCritic(
        backbone = backbone,
        actor = LinearLayerDiscreteActor([num_actions], actor_input),
        critic = LinearLayerCritic(critic_input),
    )
else:
    policy = ActorCritic(
        backbone = BackboneShared(
            process_obs = to1D,
            encoder = BackboneEncoder(lambda x: x),
        ),
        actor = PPOTabularActor(num_states, num_actions),
        critic = PPOTabularCritic(num_states),
    )

sim_iface = SimInterface(
    step = lambda: world.step(),
    obs = [world.sim.observation_tensor().to_torch()],
    actions = world.sim.action_tensor().to_torch(),
    dones = world.sim.done_tensor().to_torch(),
    rewards = world.sim.reward_tensor().to_torch(),
)

resets = world.sim.reset_tensor().to_torch()

trained = train(
    dev,
    sim_iface,
    TrainConfig(
        num_updates = args.num_updates,
        gamma = args.gamma,
        gae_lambda = 0.95,
        lr = args.lr,
        steps_per_update = args.steps_per_update,
        num_bptt_chunks = args.num_bptt_chunks,
        ppo = PPOConfig(
            num_mini_batches=1,
            clip_coef=0.2,
            value_loss_coef=args.value_loss_coef,
            entropy_coef=args.entropy_loss_coef,
            max_grad_norm=0.5,
            num_epochs=1,
            clip_value_loss=False,
        ),
        mixed_precision = args.fp16,
    ),
    policy,
    update_cb,
)

print()

V = torch.zeros(num_rows, num_cols,
                dtype=torch.float32, device=dev)
action_probs = torch.zeros(num_rows, num_cols, num_actions,
                            dtype=torch.float32, device=dev)

logits = torch.zeros(num_rows, num_cols, num_actions,
                            dtype=torch.float32, device=dev)

cur_rnn_states = []

for shape in trained.recurrent_cfg.shapes:
    cur_rnn_states.append(torch.zeros(
        *shape[0:2], 1, shape[2], dtype=torch.float32, device=dev))

with torch.no_grad():
    # Note these collected values are pretty much meaningless with a recurrent policy
    for r in range(num_rows):
        for c in range(num_cols):
            action_dist, value, cur_rnn_states = trained(
                cur_rnn_states, torch.tensor([[r, c]], dtype=torch.int32, device=dev))
            V[r, c] = value[0, 0]
            action_probs[r, c, :] = action_dist.probs()[0][0]
            logits[r, c, :] = action_dist.unnormalized_logits[0]

    for state in cur_rnn_states:
        state.zero_()

    resets[:] = 1
    sim_iface.step()

    for i in range(16):
        print("Obs:   ", sim_iface.obs[0][0])
        trained.fwd_actor(sim_iface.actions[0:1], cur_rnn_states, cur_rnn_states, sim_iface.obs[0][0:1])
        print("Action:", sim_iface.actions[0].cpu().numpy())
        sim_iface.step()
        print("Reward:", sim_iface.rewards[0].cpu().numpy())
        print()

print(f"Grid size: {num_rows} x {num_cols}")
print(rewards)
print(walls)
print("\nV:")

for r in range(num_rows):
    for c in range(num_cols):
        print(f"{V[r, c]: .2f} ", end='')
    print()

print("\nAction probs:")
for r in range(num_rows):
    for c in range(num_cols):
        probs = action_probs[r, c]
        print(f"  {r}, {c}: [{probs[0]:.2f} {probs[1]:.2f} {probs[2]:.2f} {probs[3]:.2f}]")

print("\nLogits:")
for r in range(num_rows):
    for c in range(num_cols):
        l = logits[r, c]
        print(f"  {r}, {c}: [{l[0]:.2f} {l[1]:.2f} {l[2]:.2f} {l[3]:.2f}]")

print()
if not args.dnn:
    print("\nTabular Actor:")
    for r in range(num_rows):
        for c in range(num_cols):
            flat = r * num_cols + c
            l = trained.actor.tbl.policy[flat]
            print(f"  {r}, {c}: [{l[0]:.2f} {l[1]:.2f} {l[2]:.2f} {l[3]:.2f}]")
