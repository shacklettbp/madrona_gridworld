import numpy as np
import torch
import pickle as pkl
#from gridworld import GridWorld

array_shape = [5,6]
walls = np.zeros(array_shape)
rewards = np.zeros(array_shape)
walls[3,2:] = 1
start_cell = np.array([0,0])
end_cell = np.array([[4,5]])
rewards[4,0] = -1
rewards[4,5] = 1

#grid_world = GridWorld(start_cell, end_cell, rewards, walls, num_worlds)

with open("./world_configs/test_world.pkl", 'wb') as handle:
    pkl.dump([start_cell, end_cell, rewards, walls], handle, protocol=pkl.HIGHEST_PROTOCOL)