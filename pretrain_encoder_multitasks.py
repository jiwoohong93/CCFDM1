import numpy as np
import torch
import argparse
import os
import math
import gym
import sys
import random
import time
import json
import dmc2gym
import copy
import glob
from tqdm import tqdm

import utils
from logger import Logger
from video import VideoRecorder

from curl_sac import CurlSacAgent
from curl_sac_pretrain import PretrainedSacAgent
from curl_sac_pretrain_v3 import PretrainedSacAgent_v3
from torchvision import transforms


def parse_args():
    parser = argparse.ArgumentParser()
    # environment
    parser.add_argument('--domain_name', default='multi')
    parser.add_argument('--task_name', default='domains')
    parser.add_argument('--pre_transform_image_size', default=100, type=int)

    parser.add_argument('--max_tasks', default=10, type=int)
    parser.add_argument('--action_shape', default=6, type=int)

    parser.add_argument('--image_size', default=84, type=int)
    parser.add_argument('--action_repeat', default=1, type=int)
    parser.add_argument('--frame_stack', default=3, type=int)
    # replay buffer
    parser.add_argument('--replay_buffer_capacity', default=100000, type=int)
    # train
    parser.add_argument('--agent', default='curl_sac', type=str)
    parser.add_argument('--init_steps', default=1000, type=int)
    parser.add_argument('--num_train_steps', default=1000000, type=int)
    parser.add_argument('--batch_size', default=32, type=int)
    parser.add_argument('--hidden_dim', default=1024, type=int)
    # eval
    parser.add_argument('--eval_freq', default=1000, type=int)
    parser.add_argument('--num_eval_episodes', default=10, type=int)
    # critic
    parser.add_argument('--critic_lr', default=1e-3, type=float)
    parser.add_argument('--critic_beta', default=0.9, type=float)
    parser.add_argument('--critic_tau', default=0.01, type=float) # try 0.05 or 0.1
    parser.add_argument('--critic_target_update_freq', default=2, type=int) # try to change it to 1 and retain 0.01 above
    # actor
    parser.add_argument('--actor_lr', default=1e-3, type=float)
    parser.add_argument('--actor_beta', default=0.9, type=float)
    parser.add_argument('--actor_log_std_min', default=-10, type=float)
    parser.add_argument('--actor_log_std_max', default=2, type=float)
    parser.add_argument('--actor_update_freq', default=2, type=int)
    # encoder
    parser.add_argument('--encoder_type', default='pixel', type=str)
    parser.add_argument('--encoder_feature_dim', default=50, type=int)
    parser.add_argument('--encoder_lr', default=1e-3, type=float)
    parser.add_argument('--idm_lr', default=1e-3, type=float)
    parser.add_argument('--fdm_lr', default=1e-3, type=float)
    parser.add_argument('--encoder_tau', default=0.05, type=float)
    parser.add_argument('--num_layers', default=4, type=int)
    parser.add_argument('--num_filters', default=32, type=int)

    # Self-supervised learning config
    parser.add_argument('--n_samples', default=50000, type=int)
    parser.add_argument('--cpc_update_freq', default=1, type=int)
    parser.add_argument('--idm_update_freq', default=999999999, type=int)
    parser.add_argument('--fdm_update_freq', default=999999999, type=int)

    parser.add_argument('--curl_latent_dim', default=128, type=int)
    # sac
    parser.add_argument('--discount', default=0.99, type=float)
    parser.add_argument('--init_temperature', default=0.1, type=float)
    parser.add_argument('--alpha_lr', default=1e-4, type=float)
    parser.add_argument('--alpha_beta', default=0.5, type=float)
    # misc
    parser.add_argument('--seed', default=1, type=int)
    parser.add_argument('--exp', default='exp', type=str)
    parser.add_argument('--work_dir', default='.', type=str)
    parser.add_argument('--save_tb', default=False, action='store_true')
    parser.add_argument('--save_buffer', default=False, action='store_true')
    parser.add_argument('--load_buffer', default=None, type=str)
    parser.add_argument('--save_video', default=False, action='store_true')
    parser.add_argument('--save_model', default=False, action='store_true')
    parser.add_argument('--detach_encoder', default=False, action='store_true')

    parser.add_argument('--log_interval', default=100, type=int)
    args = parser.parse_args()
    return args


def evaluate(env, agent, video, num_episodes, L, step, env_step, args):
    all_ep_rewards = []

    def run_eval_loop(sample_stochastically=True):
        start_time = time.time()
        prefix = 'stochastic_' if sample_stochastically else ''
        for i in range(num_episodes):
            obs = env.reset()
            video.init(enabled=(i == 0))
            done = False
            episode_reward = 0
            while not done:
                # center crop image
                if args.encoder_type == 'pixel':
                    obs = utils.center_crop_image(obs,args.image_size)
                with utils.eval_mode(agent):
                    if sample_stochastically:
                        action = agent.sample_action(obs)
                    else:
                        action = agent.select_action(obs)
                obs, reward, done, _ = env.step(action)
                video.record(env)
                episode_reward += reward

            video.save('%d.mp4' % step)
            L.log('eval/' + prefix + 'episode_reward', episode_reward, env_step)
            all_ep_rewards.append(episode_reward)
        
        L.log('eval/' + prefix + 'eval_time', time.time()-start_time , env_step)
        mean_ep_reward = np.mean(all_ep_rewards)
        best_ep_reward = np.max(all_ep_rewards)
        L.log('eval/' + prefix + 'mean_episode_reward', mean_ep_reward, env_step)
        L.log('eval/' + prefix + 'best_episode_reward', best_ep_reward, env_step)

    run_eval_loop(sample_stochastically=False)
    L.dump(env_step)


def make_agent(obs_shape, action_shape, args, device):
    if args.agent == 'pretrained_sac_v3':
        return PretrainedSacAgent_v3(
            obs_shape=obs_shape,
            action_shape=action_shape,
            max_tasks=args.max_tasks,
            device=device,
            hidden_dim=args.hidden_dim,
            discount=args.discount,
            init_temperature=args.init_temperature,
            alpha_lr=args.alpha_lr,
            alpha_beta=args.alpha_beta,
            actor_lr=args.actor_lr,
            actor_beta=args.actor_beta,
            actor_log_std_min=args.actor_log_std_min,
            actor_log_std_max=args.actor_log_std_max,
            actor_update_freq=args.actor_update_freq,
            critic_lr=args.critic_lr,
            critic_beta=args.critic_beta,
            critic_tau=args.critic_tau,
            critic_target_update_freq=args.critic_target_update_freq,
            encoder_type=args.encoder_type,
            encoder_feature_dim=args.encoder_feature_dim,
            encoder_lr=args.encoder_lr,
            idm_lr=args.idm_lr,
            fdm_lr=args.fdm_lr,
            encoder_tau=args.encoder_tau,
            num_layers=args.num_layers,
            num_filters=args.num_filters,
            cpc_update_freq=args.cpc_update_freq,
            idm_update_freq=args.idm_update_freq,
            fdm_update_freq=args.fdm_update_freq,
            log_interval=args.log_interval,
            detach_encoder=args.detach_encoder,
            curl_latent_dim=args.curl_latent_dim
        )
    else:
        assert 'agent is not supported: %s' % args.agent


def make_logdir(args):
    # make directory
    ts = time.localtime()
    ts = time.strftime("%m-%d-%H-%M-%S", ts)
    env_name = args.domain_name + '-' + args.task_name
    if args.encoder_type == 'pixel':
        exp_name = env_name + '/' + args.exp + '/' + 'img' + str(args.image_size) + \
                   '-b' + str(args.batch_size) + '-s' + str(args.seed) + \
                   '-' + args.encoder_type + '-' + ts
    elif args.encoder_type == 'identity':
        exp_name = env_name + '/' + args.exp + '/' + 'state' + \
                   '-b' + str(args.batch_size) + '-s' + str(args.seed) + \
                   '-' + args.encoder_type + '-' + ts
    else:
        raise NotImplementedError('Not support: {}'.format(args.encoder_type))

    args.work_dir = args.work_dir + '/' + exp_name

    utils.make_dir(args.work_dir)


def main():
    args = parse_args()
    if args.seed == -1: 
        args.__dict__["seed"] = np.random.randint(1,1000000)
    utils.set_seed_everywhere(args.seed)

    envs = []
    domain_names = ['ball_in_cup', 'cartpole', 'walker', 'cheetah']
    task_names = ['catch', 'swingup', 'walk', 'run']
    action_repeats = dict(ball_in_cup=4,
                          cartpole=8,
                          walker=2,
                          cheetah=4)
    n_tasks = len(domain_names)

    for i in range(n_tasks):
        env = dmc2gym.make(
            domain_name=domain_names[i],
            task_name=task_names[i],
            seed=args.seed,
            visualize_reward=False,
            from_pixels=(args.encoder_type == 'pixel'),
            height=args.pre_transform_image_size,
            width=args.pre_transform_image_size,
            frame_skip=action_repeats[domain_names[i]]
        )
        env.seed(args.seed)

        # stack several consecutive frames together
        if args.encoder_type == 'pixel':
            env = utils.FrameStack(env, k=args.frame_stack)

        envs.append(env)

    # make directory
    make_logdir(args)
    video_dir = utils.make_dir(os.path.join(args.work_dir, 'video'))
    model_dir = utils.make_dir(os.path.join(args.work_dir, 'model'))
    buffer_dir = utils.make_dir(os.path.join(args.work_dir, 'buffer'))

    video = VideoRecorder(video_dir if args.save_video else None)

    with open(os.path.join(args.work_dir, 'args.json'), 'w') as f:
        json.dump(vars(args), f, sort_keys=True, indent=4)

    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')

    action_shape = (args.action_shape, )    # TODO: Hard-code, fix this later

    if args.encoder_type == 'pixel':
        obs_shape = (3*args.frame_stack, args.image_size, args.image_size)
        pre_aug_obs_shape = (3*args.frame_stack,args.pre_transform_image_size,args.pre_transform_image_size)
    else:
        obs_shape = env.observation_space.shape
        pre_aug_obs_shape = obs_shape

    replay_buffer = utils.ReplayBufferMultiTasks(
        obs_shape=pre_aug_obs_shape,
        action_shape=(args.action_shape, ),
        task_shape=(args.max_tasks, ),
        capacity=args.replay_buffer_capacity * n_tasks,
        batch_size=args.batch_size,
        device=device,
        image_size=args.image_size,
    )

    agent = make_agent(
        obs_shape=obs_shape,
        action_shape=action_shape,
        args=args,
        device=device
    )

    L = Logger(args.work_dir, use_tb=args.save_tb)

    episode, done = 0, True

    if args.load_buffer is not None:
        save_dir = os.path.join(args.load_buffer, 'buffer')
        replay_buffer.load(save_dir)
    else:
        print('[INFO] Collecting data from environment...')
        for n in range(n_tasks):
            for _ in tqdm(range(args.n_samples)):
                # sampled_task = np.random.randint(n_tasks)
                sampled_task = n
                task_desc = np.zeros(args.max_tasks, dtype=np.float32)
                task_desc[sampled_task] = 1.0

                if done:
                    obs = envs[sampled_task].reset()
                    done = False
                    episode_step = 0
                    episode += 1

                action = envs[sampled_task].action_space.sample()
                next_obs, reward, done, _ = envs[sampled_task].step(action)

                # allow infinit bootstrap
                done_bool = 0 if episode_step + 1 == envs[sampled_task]._max_episode_steps else float(
                    done
                )
                replay_buffer.add(obs, action, reward, next_obs, done_bool, task_desc)

                obs = next_obs
                episode_step += 1
            replay_buffer.save(buffer_dir)

    #if args.save_buffer and args.load_buffer is None:
    #    replay_buffer.save(buffer_dir)

    print('[INFO] Pre-training encoder ...')
    for step in tqdm(range(args.num_train_steps + 1)):
        # evaluate agent periodically

        if step != 0:
            agent.update(replay_buffer, L, step)

        if step % args.eval_freq == 0:
            print('[INFO] Experiment: {} - seed: {}'.format(args.exp, args.seed))
            if args.save_model:
                agent.save_curl(model_dir, step)
                agent.save(model_dir, step)



if __name__ == '__main__':
    torch.multiprocessing.set_start_method('spawn')

    main()