import logging

from collections import defaultdict, OrderedDict
import gym
from gym import spaces
from numpy.lib.utils import info

from robotic_warehouse.utils import MultiAgentActionSpace, MultiAgentObservationSpace

from enum import Enum
import numpy as np

from typing import List, Tuple, Optional, Dict

import networkx as nx

_AXIS_Z = 0
_AXIS_Y = 1
_AXIS_X = 2

_COLLISION_LAYERS = 2

_LAYER_AGENTS = 0
_LAYER_SHELFS = 1


class _VectorWriter:
    def __init__(self, size: int):
        self.vector = np.zeros(size, dtype=np.float32)
        self.idx = 0

    def write(self, data):
        data_size = len(data)
        self.vector[self.idx : self.idx + data_size] = data
        self.idx += data_size

    def skip(self, bits):
        self.idx += bits


class Action(Enum):
    NOOP = 0
    FORWARD = 1
    LEFT = 2
    RIGHT = 3
    TOGGLE_LOAD = 4


class Direction(Enum):
    UP = 0
    DOWN = 1
    LEFT = 2
    RIGHT = 3


class RewardType(Enum):
    GLOBAL = 0
    INDIVIDUAL = 1
    TWO_STAGE = 2


class Entity:
    def __init__(self, id_: int, x: int, y: int):
        self.id = id_
        self.prev_x = None
        self.prev_y = None
        self.x = x
        self.y = y


class Agent(Entity):
    counter = 0

    def __init__(self, x: int, y: int, dir_: Direction, msg_bits: int):
        Agent.counter += 1
        super().__init__(Agent.counter, x, y)
        self.dir = dir_
        self.message = np.zeros(msg_bits)
        self.req_action: Optional[Action] = None
        self.carrying_shelf: Optional[Shelf] = None
        self.canceled_action = None
        self.has_delivered = False

    @property
    def collision_layers(self):
        if self.loaded:
            return (_LAYER_AGENTS, _LAYER_SHELFS)
        else:
            return (_LAYER_AGENTS,)

    def req_location(self, grid_size) -> Tuple[int, int]:
        if self.req_action != Action.FORWARD:
            return self.x, self.y
        elif self.dir == Direction.UP:
            return self.x, max(0, self.y - 1)
        elif self.dir == Direction.DOWN:
            return self.x, min(grid_size[0] - 1, self.y + 1)
        elif self.dir == Direction.LEFT:
            return max(0, self.x - 1), self.y
        elif self.dir == Direction.RIGHT:
            return min(grid_size[1] - 1, self.x + 1), self.y

        raise ValueError(
            f"Direction is {self.dir}. Should be one of {[v for v in Direction]}"
        )

    def req_direction(self) -> Direction:
        wraplist = [Direction.UP, Direction.RIGHT, Direction.DOWN, Direction.LEFT]
        if self.req_action == Action.RIGHT:
            return wraplist[(wraplist.index(self.dir) + 1) % len(wraplist)]
        elif self.req_action == Action.LEFT:
            return wraplist[(wraplist.index(self.dir) - 1) % len(wraplist)]
        else:
            return self.dir


class Shelf(Entity):
    counter = 0

    def __init__(self, x, y):
        Shelf.counter += 1
        super().__init__(Shelf.counter, x, y)

    @property
    def collision_layers(self):
        return (_LAYER_SHELFS,)


class Warehouse(gym.Env):

    metadata = {"render.modes": ["human", "rgb_array"]}

    def __init__(
        self,
        shelf_columns: int,
        column_height: int,
        shelf_rows: int,
        n_agents: int,
        msg_bits: int,
        sensor_range: int,
        request_queue_size: int,
        max_inactivity_steps: Optional[int],
        max_steps: Optional[int],
        reward_type: RewardType,
        fast_obs=True,
    ):
        """The robotic warehouse environment

        Creates a grid world where multiple agents (robots)
        are supposed to collect shelves, bring them to a goal
        and then return them.
        .. note:
            The grid looks like this:

            shelf
            columns
                vv
            ----------
            -XX-XX-XX-        ^
            -XX-XX-XX-  Column Height
            -XX-XX-XX-        v
            ----------
            -XX----XX-   <\
            -XX----XX-   <- Shelf Rows
            -XX----XX-   </
            ----------
            ----GG----

            G: is the goal positions where agents are rewarded if
            they bring the correct shelfs.

            The final grid size will be
            height: (column_height + 1) * shelf_rows + 2
            width: (2 + 1) * shelf_columns + 1

            The bottom-middle column will be removed to allow for
            robot queuing next to the goal locations

        :param shelf_columns: Number of columns in the warehouse
        :type shelf_columns: int
        :param column_height: Column height in the warehouse
        :type column_height: int
        :param shelf_rows: Number of columns in the warehouse
        :type shelf_rows: int
        :param n_agents: Number of spawned and controlled agents
        :type n_agents: int
        :param msg_bits: Number of communication bits for each agent
        :type msg_bits: int
        :param sensor_range: Range of each agents observation
        :type sensor_range: int
        :param request_queue_size: How many shelfs are simultaneously requested
        :type request_queue_size: int
        :param max_inactivity: Number of steps without a delivered shelf until environment finishes
        :type max_inactivity: Optional[int]
        :param reward_type: Specifies if agents are rewarded individually or globally
        :type reward_type: RewardType
        """

        assert shelf_columns % 2 == 1, "Only odd number of shelf columns is supported"

        self.grid_size = (
            (column_height + 1) * shelf_rows + 2,
            (2 + 1) * shelf_columns + 1,
        )

        self.n_agents = n_agents
        self.msg_bits = msg_bits
        self.sensor_range = sensor_range
        self.max_inactivity_steps: Optional[int] = max_inactivity_steps
        self.reward_type = reward_type
        # self.reward_range = (0, 1)
        self.reward_range = (-float("inf"), float("inf"))

        self._cur_inactive_steps = None
        self._cur_steps = 0
        self.max_steps = max_steps

        self.grid = np.zeros((_COLLISION_LAYERS, *self.grid_size), dtype=np.int32)

        sa_action_space = [len(Action), *msg_bits * (2,)]
        if len(sa_action_space) == 1:
            sa_action_space = spaces.Discrete(sa_action_space[0])
        else:
            sa_action_space = spaces.MultiDiscrete(sa_action_space)
        self.action_space = spaces.Tuple(tuple(n_agents * [sa_action_space]))

        self.request_queue_size = request_queue_size
        self.request_queue = []

        self.requested_delivered_shelf = []
        self.carried_shelf = []

        # self.carried_delivered_shelf = []
        # self.carried_requested_shelf = []        
        # self.requested_undelivered_shelf = []

        self.agents: List[Agent] = []

        # self.goals: List[Tuple[int, int]] = [
        #     (self.grid_size[1] // 2 - 1, self.grid_size[0] - 1),
        #     (self.grid_size[1] // 2, self.grid_size[0] - 1),
        # ]

        # self.goals: List[Tuple[int, int]] = [
        #     (self.grid_size[1] // 2 - 1, self.grid_size[0] - 1),
        #     (self.grid_size[1] // 2, self.grid_size[0] - 1),
        #     (self.grid_size[1] // 2 - 2, self.grid_size[0] - 1),
        #     (self.grid_size[1] // 2 + 1, self.grid_size[0] - 1),
        #     (self.grid_size[1] - 1, self.grid_size[0] - 1),
        #     (self.grid_size[1] - 2, self.grid_size[0] - 1),
        #     (self.grid_size[1] - 3, self.grid_size[0] - 1),
        #     (self.grid_size[1] - 4, self.grid_size[0] - 1),
        #     (0, self.grid_size[0] - 1),
        #     (1, self.grid_size[0] - 1),
        #     (2, self.grid_size[0] - 1),
        #     (3, self.grid_size[0] - 1)
        # ]

        self.goals: List[Tuple[int, int]] = [(i, self.grid_size[0] - 1) for i in range(self.grid_size[1])]

        self._obs_bits_for_self = 4 + len(Direction)
        self._obs_bits_per_agent = 1 + len(Direction) + self.msg_bits
        self._obs_bits_per_shelf = 2
        self._obs_bits_for_requests = 2

        self._obs_sensor_locations = (1 + 2 * self.sensor_range) ** 2

        self._obs_length = (
            self._obs_bits_for_self
            + self._obs_sensor_locations * self._obs_bits_per_agent
            + self._obs_sensor_locations * self._obs_bits_per_shelf
        )

        # default values:
        self.fast_obs = None
        self.observation_space = None
        self._use_slow_obs()

        # for performance reasons we
        # can flatten the obs vector
        if fast_obs:
            self._use_fast_obs()

        self.renderer = None

    def _use_slow_obs(self):
        self.fast_obs = False
        self.observation_space = spaces.Tuple(
            tuple(
                [
                    spaces.Dict(
                        OrderedDict(
                            {
                                "self": spaces.Dict(
                                    OrderedDict(
                                        {
                                            "location": spaces.MultiDiscrete(
                                                [self.grid_size[1], self.grid_size[0]]
                                            ),
                                            "carrying_shelf": spaces.MultiDiscrete([2]),
                                            "direction": spaces.Discrete(4),
                                            "on_highway": spaces.MultiDiscrete([2]),
                                        }
                                    )
                                ),
                                "sensors": spaces.Tuple(
                                    self._obs_sensor_locations
                                    * (
                                        spaces.Dict(
                                            OrderedDict(
                                                {
                                                    "has_agent": spaces.MultiDiscrete(
                                                        [2]
                                                    ),
                                                    "direction": spaces.Discrete(4),
                                                    "local_message": spaces.MultiBinary(
                                                        self.msg_bits
                                                    ),
                                                    "has_shelf": spaces.MultiDiscrete(
                                                        [2]
                                                    ),
                                                    "shelf_requested": spaces.MultiDiscrete(
                                                        [2]
                                                    ),
                                                }
                                            )
                                        ),
                                    )
                                ),
                            }
                        )
                    )
                    for _ in range(self.n_agents)
                ]
            )
        )

    def _use_fast_obs(self):
        if self.fast_obs:
            return

        self.fast_obs = True
        ma_spaces = []
        for sa_obs in self.observation_space:
            flatdim = spaces.flatdim(sa_obs)
            ma_spaces += [
                spaces.Box(
                    low=-float("inf"),
                    high=float("inf"),
                    shape=(flatdim,),
                    dtype=np.float32,
                )
            ]

        self.observation_space = spaces.Tuple(tuple(ma_spaces))

    def _is_highway(self, x: int, y: int) -> bool:
        return (
            (x % 3 == 0)  # vertical highways
            or (y % 9 == 0)  # horizontal highways
            or (y == self.grid_size[0] - 1)  # delivery row
            or (  # remove a box for queuing
                (y > self.grid_size[0] - 11)
                and ((x == self.grid_size[1] // 2 - 1) or (x == self.grid_size[1] // 2))
            )
        )

    def _make_obs(self, agent):

        y_scale, x_scale = self.grid_size[0] - 1, self.grid_size[1] - 1

        min_x = agent.x - self.sensor_range
        max_x = agent.x + self.sensor_range + 1

        min_y = agent.y - self.sensor_range
        max_y = agent.y + self.sensor_range + 1
        # sensors
        if (
            (min_x < 0)
            or (min_y < 0)
            or (max_x > self.grid_size[1])
            or (max_y > self.grid_size[0])
        ):
            padded_agents = np.pad(
                self.grid[_LAYER_AGENTS], self.sensor_range, mode="constant"
            )
            padded_shelfs = np.pad(
                self.grid[_LAYER_SHELFS], self.sensor_range, mode="constant"
            )
            # + self.sensor_range due to padding
            min_x += self.sensor_range
            max_x += self.sensor_range
            min_y += self.sensor_range
            max_y += self.sensor_range

        else:
            padded_agents = self.grid[_LAYER_AGENTS]
            padded_shelfs = self.grid[_LAYER_SHELFS]

        agents = padded_agents[min_y:max_y, min_x:max_x].reshape(-1)
        shelfs = padded_shelfs[min_y:max_y, min_x:max_x].reshape(-1)

        if self.fast_obs:
            obs = _VectorWriter(self.observation_space[agent.id - 1].shape[0])

            obs.write([agent.x, agent.y, int(agent.carrying_shelf is not None)])
            direction = np.zeros(4)
            direction[agent.dir.value] = 1.0
            obs.write(direction)
            obs.write([int(self._is_highway(agent.x, agent.y))])

            for i, (id_agent, id_shelf) in enumerate(zip(agents, shelfs)):
                if id_agent == 0:
                    obs.skip(1)
                    obs.write([1.0])
                    obs.skip(3 + self.msg_bits)
                else:
                    obs.write([1.0])
                    direction = np.zeros(4)
                    direction[self.agents[id_agent - 1].dir.value] = 1.0
                    obs.write(direction)
                    if self.msg_bits > 0:
                        obs.write(self.agents[id_agent - 1].message)
                if id_shelf == 0:
                    obs.skip(2)
                else:
                    obs.write(
                        [1.0, int(self.shelfs[id_shelf - 1] in self.request_queue)]
                    )

            return obs.vector

        # --- self data
        obs = {}
        obs["self"] = {
            "location": np.array([agent.x, agent.y]),
            "carrying_shelf": [int(agent.carrying_shelf is not None)],
            "direction": agent.dir.value,
            "on_highway": [int(self._is_highway(agent.x, agent.y))],
        }
        # --- sensor data
        obs["sensors"] = tuple({} for _ in range(self._obs_sensor_locations))

        # find neighboring agents
        for i, id_ in enumerate(agents):
            if id_ == 0:
                obs["sensors"][i]["has_agent"] = [0]
                obs["sensors"][i]["direction"] = 0
                obs["sensors"][i]["local_message"] = self.msg_bits * [0]
            else:
                obs["sensors"][i]["has_agent"] = [1]
                obs["sensors"][i]["direction"] = self.agents[id_ - 1].dir.value
                obs["sensors"][i]["local_message"] = self.agents[id_ - 1].message

        # find neighboring shelfs:
        for i, id_ in enumerate(shelfs):
            if id_ == 0:
                obs["sensors"][i]["has_shelf"] = [0]
                obs["sensors"][i]["shelf_requested"] = [0]
            else:
                obs["sensors"][i]["has_shelf"] = [1]
                obs["sensors"][i]["shelf_requested"] = [
                    int(self.shelfs[id_ - 1] in self.request_queue)
                ]

        return obs

    def _recalc_grid(self):
        self.grid[:] = 0
        for s in self.shelfs:
            self.grid[_LAYER_SHELFS, s.y, s.x] = s.id

        for a in self.agents:
            self.grid[_LAYER_AGENTS, a.y, a.x] = a.id

    def reset(self):
        Shelf.counter = 0
        Agent.counter = 0
        self._cur_inactive_steps = 0
        self._cur_steps = 0

        # n_xshelf = (self.grid_size[1] - 1) // 3
        # n_yshelf = (self.grid_size[0] - 2) // 9

        # make the shelfs
        self.shelfs = [
            Shelf(x, y)
            for y, x in zip(
                np.indices(self.grid_size)[0].reshape(-1),
                np.indices(self.grid_size)[1].reshape(-1),
            )
            if not self._is_highway(x, y)
        ]

        # spawn agents at random locations
        agent_locs = np.random.choice(
            np.arange(self.grid_size[0] * self.grid_size[1]),
            size=self.n_agents,
            replace=False,
        )
        agent_locs = np.unravel_index(agent_locs, self.grid_size)
        # and direction
        agent_dirs = np.random.choice([d for d in Direction], size=self.n_agents)
        self.agents = [
            Agent(x, y, dir_, self.msg_bits)
            for y, x, dir_ in zip(*agent_locs, agent_dirs)
        ]

        self._recalc_grid()

        self.shelf_original_coordinates = {s.id:[s.y, s.x] for s in self.shelfs}

        self.shelf_original_dist_goal = \
            {s.id:min(abs(s.x - list(self.goals[0])[0]), abs(s.x - list(self.goals[1])[0])) \
                + abs(s.y - list(self.goals[0])[1]) for s in self.shelfs}

        self.request_queue = list(
            np.random.choice(self.shelfs, size=self.request_queue_size, replace=False)
        )

        return tuple([self._make_obs(agent) for agent in self.agents])
        # for s in self.shelfs:
        #     self.grid[0, s.y, s.x] = 1
        # print(self.grid[0])
    
    
    def _reward(self, pos, goal, d):
        """
        Compute the reward to be given 
        """
        if np.linalg.norm(pos - goal, ord=1) < d:
            reward = 0
        else: 
            reward = - np.linalg.norm(pos - goal, ord=1) / (self.grid_size[0] * self.grid_size[1])
        return reward
    
    def dist_pos_goal(self, pos, goal):
        return np.linalg.norm(pos - goal, ord=1)


    def shelf_ids_coordinates(self, shelf_list):
        """
        Compute the shelf ids and their coordinates
        """
        ids = [shelf.id for shelf in shelf_list]
        coordinates = \
            [np.concatenate(np.where(self.grid[_LAYER_SHELFS] == shelf_id)) for shelf_id in ids]
        return ids, coordinates

    
    def update_shelf_properties(self):
        ## Shelves can be (1) requested or unrequested; (2) carried or uncarried; 
        ## (3) delivered or undelivered (for requested only)

        self.requested_shelf_ids, self.requested_shelf_coordinates = \
            self.shelf_ids_coordinates(self.request_queue)
        # print("requested:", self.requested_shelf_ids)
        

        self.unrequested_shelf = list(set(self.shelfs) - set(self.request_queue))
        self.unrequested_shelf_ids, self.unrequested_shelf_coordinates = \
            self.shelf_ids_coordinates(self.unrequested_shelf)
        # print("unrequested:", self.requested_shelf_ids) 
        

        ## *** update in each step
        if not len(self.requested_delivered_shelf):
            self.requested_delivered_shelf_ids, self.requested_delivered_shelf_coordinates = \
                self.shelf_ids_coordinates(self.requested_delivered_shelf)
        else: 
            self.requested_delivered_shelf_ids, self.requested_delivered_shelf_coordinates = None, None
        # print("requested and delivered", self.requested_delivered_shelf_ids)
        

        self.requested_undelivered_shelf = list(set(self.request_queue) - set(self.requested_delivered_shelf))
        self.requested_undelivered_shelf_ids, self.requested_undelivered_shelf_coordinates = \
            self.shelf_ids_coordinates(self.requested_undelivered_shelf)
        # print("requested and undelivered", self.requested_undelivered_shelf_ids)
        

        ## *** update in each step
        if not len(self.carried_shelf):
            self.carried_shelf_ids, self.carried_shelf_coordinates = \
                self.shelf_ids_coordinates(self.carried_shelf)
        else:
            self.carried_shelf_ids, self.carried_shelf_coordinates = None, None
        # print("carried", self.carried_shelf_ids)


        self.uncarried_shelf = list(set(self.shelfs) - set(self.carried_shelf))
        self.uncarried_shelf_ids, self.unrcarried_shelf_coordinates = \
            self.shelf_ids_coordinates(self.uncarried_shelf)
        # print("uncarried", self.uncarried_shelf_ids)



        self.carried_delivered_shelf = list(set(self.carried_shelf) & set(self.requested_delivered_shelf))  
        self.carried_delivered_shelf_ids, self.carried_delivered_shelf_coordinates = \
            self.shelf_ids_coordinates(self.carried_delivered_shelf)   
        # print("carried and delivered:", self.carried_delivered_shelf_ids)

        self.carried_undelivered_shelf = list(set(self.carried_shelf) & set(self.requested_undelivered_shelf))  
        self.carried_undelivered_shelf_ids, self.carried_delivered_shelf_coordinates = \
            self.shelf_ids_coordinates(self.carried_undelivered_shelf)
        # print("carried and undelivered:", self.carried_undelivered_shelf_ids)


        self.uncarried_delivered_shelf = list(set(self.requested_delivered_shelf) - set(self.carried_delivered_shelf)) 
        self.uncarried_delivered_shelf_ids, self.uncarried_delivered_shelf_coordinates = \
            self.shelf_ids_coordinates(self.uncarried_delivered_shelf)
        # print("uncarried and delivered:", self.uncarried_delivered_shelf_ids)
        

        self.uncarried_undelivered_shelf = list(set(self.requested_undelivered_shelf) - set(self.uncarried_delivered_shelf)) 
        self.uncarried_undelivered_shelf_ids, self.uncarried_undelivered_shelf_coordinates = \
            self.shelf_ids_coordinates(self.uncarried_undelivered_shelf)
        # print("uncarried and undelivered:", self.uncarried_undelivered_shelf_ids)
        

        self.carried_requested_shelf = list(set(self.carried_shelf) & set(self.requested_undelivered_shelf)) 
        self.carried_requested_shelf_ids, self.carried_requested_shelf_coordinates = \
            self.shelf_ids_coordinates(self.carried_requested_shelf)
        # self.carried_requested_shelf_ids = [shelf.id for shelf in self.carried_request_shelf]
        # print("carried and requested:", self.carried_requested_shelf_ids)


        self.uncarried_requested_shelf = list(set(self.uncarried_shelf) & set(self.requested_undelivered_shelf)) 
        self.uncarried_requested_shelf_ids, self.uncarried_requested_shelf_coordinates = \
            self.shelf_ids_coordinates(self.uncarried_requested_shelf)
        # print("uncarried and requested:", self.uncarried_requested_shelf_ids)


    def nonsparse_reward(self, agent, pos, goals, dist, rewards):
    ## Add the newly designed rewards (non-sparse)            
        if agent.carrying_shelf:
            if agent.carrying_shelf in self.carried_undelivered_shelf:
                if agent.carrying_shelf in self.request_queue:
                    ## Carrying a requested shelf which is undelivered
                    ## Go to the goal location ASAP
                    if self.reward_type == RewardType.GLOBAL:
                        rewards += max([self._reward(pos, goal, dist) for goal in goals])
                    elif self.reward_type == RewardType.INDIVIDUAL:
                        agent_id = self.grid[_LAYER_AGENTS, agent.y, agent.x]
                        rewards[agent_id - 1] += max([self._reward(pos, goal, dist) for goal in goals])
                    elif self.reward_type == RewardType.TWO_STAGE:
                        agent_id = self.grid[_LAYER_AGENTS, agent.y, agent.x]
                        rewards[agent_id - 1] += max([self._reward(pos, goal, dist) for goal in goals])
                # else: 
                #     ## Carrying an unrequested shelf (which is definitely undelivered)
                #     ## Undesirable behavior; assign negative reward
                #     if self.reward_type == RewardType.GLOBAL:
                #         rewards += -2
                #     elif self.reward_type == RewardType.INDIVIDUAL:
                #         agent_id = self.grid[_LAYER_AGENTS, agent.y, agent.x]
                #         rewards[agent_id - 1] += -2
                #     elif self.reward_type == RewardType.TWO_STAGE:
                #         agent_id = self.grid[_LAYER_AGENTS, agent.y, agent.x]
                #         rewards[agent_id - 1] += -1
            else: 
                ## Carrying a delivered shelf
                ## Return the delivered shelf to an empty shelf location ASAP
                if len(self.carried_requested_shelf):
                    reward = max([self._reward(pos, coord, dist) \
                        for coord in (self.carried_requested_shelf_coordinates)])
                else: 
                    reward = 0
                if self.reward_type == RewardType.GLOBAL:
                    rewards += reward
                elif self.reward_type == RewardType.INDIVIDUAL:
                    agent_id = self.grid[_LAYER_AGENTS, agent.y, agent.x]
                    rewards[agent_id - 1] += reward
                elif self.reward_type == RewardType.TWO_STAGE:
                    agent_id = self.grid[_LAYER_AGENTS, agent.y, agent.x]
                    rewards[agent_id - 1] += reward                                                   
        else: 
            ## Not carrying any shelf
            ## Go to the closest uncarried requested shelf ASAP
            # if len(self.uncarried_request_shelf_ids):
            #     reward = max([self._reward(pos, coord, dist) for coord in self.uncarried_requested_shelf_coordinates])
            # else: 
            #     reward = 0
            reward = max([self._reward(pos, coord, dist) for coord in self.uncarried_requested_shelf_coordinates])
            if self.reward_type == RewardType.GLOBAL:
                rewards += reward
            elif self.reward_type == RewardType.INDIVIDUAL:
                agent_id = self.grid[_LAYER_AGENTS, agent.y, agent.x]
                rewards[agent_id - 1] += reward
            elif self.reward_type == RewardType.TWO_STAGE:
                agent_id = self.grid[_LAYER_AGENTS, agent.y, agent.x]
                rewards[agent_id - 1] += reward

        return rewards


    def step(
        self, actions: List[Action]
    ) -> Tuple[List[np.ndarray], List[float], List[bool], Dict]:
        assert len(actions) == len(self.agents)

        for agent, action in zip(self.agents, actions):
            if self.msg_bits > 0:
                agent.req_action = Action(action[0])
                agent.message[:] = action[1:]
            else:
                agent.req_action = Action(action)

        # # stationary agents will certainly stay where they are
        # stationary_agents = [agent for agent in self.agents if agent.action != Action.FORWARD]

        # # forward agents will move only if they avoid collisions
        # forward_agents = [agent for agent in self.agents if agent.action == Action.FORWARD]
        commited_agents = set()

        G = nx.DiGraph()

        for agent in self.agents:
            start = agent.x, agent.y
            target = agent.req_location(self.grid_size)

            if (
                agent.carrying_shelf
                and start != target
                and self.grid[_LAYER_SHELFS, target[1], target[0]]
                and not (
                    self.grid[_LAYER_AGENTS, target[1], target[0]]
                    and self.agents[
                        self.grid[_LAYER_AGENTS, target[1], target[0]] - 1
                    ].carrying_shelf
                )
            ):
                # there's a standing shelf at the target location
                # our agent is carrying a shelf so there's no way
                # this movement can succeed. Cancel it.
                agent.req_action = Action.NOOP
                G.add_edge(start, start)
            else:
                G.add_edge(start, target)

        wcomps = [G.subgraph(c).copy() for c in nx.weakly_connected_components(G)]

        for comp in wcomps:
            try:
                # if we find a cycle in this component we have to
                # commit all nodes in that cycle, and nothing else
                cycle = nx.algorithms.find_cycle(comp)
                if len(cycle) == 2:
                    # we have a situation like this: [A] <-> [B]
                    # which is physically impossible. so skip
                    continue
                for edge in cycle:
                    start_node = edge[0]
                    agent_id = self.grid[_LAYER_AGENTS, start_node[1], start_node[0]]
                    if agent_id > 0:
                        commited_agents.add(agent_id)
            except nx.NetworkXNoCycle:

                longest_path = nx.algorithms.dag_longest_path(comp)
                for x, y in longest_path:
                    agent_id = self.grid[_LAYER_AGENTS, y, x]
                    if agent_id:
                        commited_agents.add(agent_id)

        commited_agents = set([self.agents[id_ - 1] for id_ in commited_agents])
        failed_agents = set(self.agents) - commited_agents

        for agent in failed_agents:
            assert agent.req_action == Action.FORWARD
            agent.req_action = Action.NOOP

        rewards = np.zeros(self.n_agents)
        

        
        goals = np.array([list(self.goals[0]), list(self.goals[1])]) # coordinates of the goal locations
        # dist = self.grid_size[0] * self.grid_size[1]
        # dist = 0

        # print("Step ", self._cur_steps)

        # self.update_shelf_properties()
            
        self._recalc_grid()

        _, self.requested_shelf_coordinates = self.shelf_ids_coordinates(self.request_queue)
        self.requested_shelf_coordinates_tuple = [tuple(coord) for coord in self.requested_shelf_coordinates]

        for agent in self.agents:
            agent.prev_x, agent.prev_y = agent.x, agent.y

            if agent.req_action == Action.FORWARD:
                agent.x, agent.y = agent.req_location(self.grid_size)
                if agent.carrying_shelf:
                    agent.carrying_shelf.x, agent.carrying_shelf.y = agent.x, agent.y                
            elif agent.req_action in [Action.LEFT, Action.RIGHT]:
                agent.dir = agent.req_direction()
            # elif agent.req_action == Action.TOGGLE_LOAD and not agent.carrying_shelf:
            elif agent.req_action == Action.TOGGLE_LOAD and not agent.carrying_shelf \
                and (agent.y, agent.x) in self.requested_shelf_coordinates_tuple:
                shelf_id = self.grid[_LAYER_SHELFS, agent.y, agent.x]
                if shelf_id:
                    agent.carrying_shelf = self.shelfs[shelf_id - 1]
                if agent.carrying_shelf and agent.carrying_shelf in self.request_queue:                     
                    if self.reward_type == RewardType.GLOBAL:
                        rewards += 1
                    elif self.reward_type == RewardType.INDIVIDUAL:
                        agent_id = self.grid[_LAYER_AGENTS, agent.y, agent.x]
                        rewards[agent_id - 1] += 1
                    elif self.reward_type == RewardType.TWO_STAGE:
                        agent_id = self.grid[_LAYER_AGENTS, agent.y, agent.x]
                        self.agents[agent_id - 1].has_delivered = True          
                        rewards[agent_id - 1] += 0.5 
                self.carried_shelf.append(agent.carrying_shelf)               
            elif agent.req_action == Action.TOGGLE_LOAD and agent.carrying_shelf:           
                if not self._is_highway(agent.x, agent.y): 
                    # if (agent.x, agent.y) == self.goals and agent.carrying_shelf in self.carried_delivered_shelf:
                    #     self.carried_delivered_shelf.remove(agent.carrying_shelf)  
                    #     self.uncarried_delivered_shelf.append(agent.carrying_shelf)
                    if agent.carrying_shelf not in self.request_queue: 
                        self.carried_shelf.remove(agent.carrying_shelf)         
                        agent.carrying_shelf = None           
                    ## might need to change this         
                    # if agent.has_delivered and self.reward_type == RewardType.TWO_STAGE:
                    #     rewards[agent.id - 1] += 0.5 * 2
                        # rewards[agent.id - 1] += (1 - 0.9 * self._cur_steps / self.max_steps) / 2                   
                    agent.has_delivered = False          
            
            # if agent.carrying_shelf:
            #     carry_shelf_id, _ = self.shelf_original_coordinates([agent.carrying_shelf])
            #     carrying_shelf_y = self.shelf_original_coordinates[carry_shelf_id]
            #     carrying_shelf_x = self.shelf_original_coordinates[carry_shelf_id]

        
            
            # self.update_shelf_properties()
            # rewards = self.nonsparse_reward(agent, pos, goals, dist, rewards)



        self._recalc_grid()



        

        shelf_delivered = False
        for x, y in self.goals:
            shelf_id = self.grid[_LAYER_SHELFS, y, x]
            if not shelf_id:
                continue
            shelf = self.shelfs[shelf_id - 1]

            if shelf not in self.request_queue:
                continue
            # a shelf was successfully delivered.
            shelf_delivered = True


            ###
            self.carried_shelf.remove(shelf)

            for agent in self.agents:
                if agent.carrying_shelf and (agent.carrying_shelf.x, agent.carrying_shelf.y) == (x, y):                    
                    agent.has_delivered = True
                    agent.carrying_shelf = None
                    

            self.grid[_LAYER_SHELFS, y, x] = 0     
            shelf.y = self.shelf_original_coordinates[shelf_id][0]
            shelf.x = self.shelf_original_coordinates[shelf_id][1]
            self.grid[_LAYER_SHELFS, shelf.y, shelf.x] = shelf_id
            
            
            # print(self.grid[_LAYER_SHELFS, self.shelf_original_coordinates[shelf_id][0], self.shelf_original_coordinates[shelf_id][1]])
            # print(self.grid[_LAYER_SHELFS])

            self.requested_delivered_shelf.append(shelf)
            self.requested_delivered_shelf = list(set(self.requested_delivered_shelf))

            
            # self.carried_delivered_shelf.append(shelf)
            # self.carried_delivered_shelf = list(set(self.carried_delivered_shelf))
            # remove from queue and replace it
            new_request = np.random.choice(
                list(set(self.shelfs) - set(self.request_queue))
            )

            # if shelf in self.carried_requested_shelf:
            #     self.carried_requested_shelf.remove(shelf) 

            self.request_queue[self.request_queue.index(shelf)] = new_request

            # Also reward the agents based on negative distances
            # **originally only reward the agents when the shelf has been delivered**
            ## Keep the following sparse rewards

            if self.reward_type == RewardType.GLOBAL:
                # rewards += 1 - 0.9 * self._cur_steps / self.max_steps
                rewards += 1 * 2
            elif self.reward_type == RewardType.INDIVIDUAL:
                agent_id = self.grid[_LAYER_AGENTS, y, x]
                rewards[agent_id - 1] += 1 * 2
                # rewards[agent_id - 1] += 1 - 0.9 * self._cur_steps / self.max_steps
            elif self.reward_type == RewardType.TWO_STAGE:
                agent_id = self.grid[_LAYER_AGENTS, y, x]
                self.agents[agent_id - 1].has_delivered = True
                rewards[agent_id - 1] += 0.5 * 2         
                # rewards[agent_id - 1] += (1 - 0.9 * self._cur_steps / self.max_steps) / 2

        self.update_shelf_properties()
        # print(self.uncarried_requested_shelf_ids)


        '''
        _, self.requested_shelf_coordinates = self.shelf_ids_coordinates(self.request_queue)

        ## Coordinating the closest requested shelf to each agent        
        self.empty_agents = [agent for agent in self.agents if not agent.carrying_shelf]
        # self.n_empty_agents = len(self.empty_agents)
        # self.n_uncarried_requested_shelves = len(self.uncarried_requested_shelf)
        

        self.dist_empty_agents_uncarried_requested_shelves = \
            np.array([[self.dist_pos_goal(np.array([agent.y, agent.x]), coord) for agent in self.empty_agents]\
                 for coord in self.uncarried_requested_shelf_coordinates])
        
        self.dist_goals_uncarried_requested_shelves = \
            np.array([min([self.dist_pos_goal(goal, coord) for goal in goals])\
                 for coord in self.uncarried_requested_shelf_coordinates])

        # print("dist of goal and uncarried requested shelves", self.dist_goals_uncarried_requested_shelves)
        # print(self.dist_empty_agents_uncarried_requested_shelves)
        # if self.dist_empty_agents_uncarried_requested_shelves:
        #     print(np.argmin(self.dist_empty_agents_uncarried_requested_shelves, axis=1))

        # uncarried requested shelves which are farther from the goal locations have higher priority
        # print(np.argmax(self.dist_goals_uncarried_requested_shelves, axis=))


        
        for agent in self.agents:
            pos = np.array([agent.y, agent.x]) # coordinates of the agent
            if agent.prev_y and agent.prev_x:
                prev_pos = np.array([agent.prev_y, agent.prev_x])
            if agent.carrying_shelf and agent.carrying_shelf in self.request_queue: 
                min_dist_pos_goal = min([self.dist_pos_goal(pos, goal) for goal in goals])
                if agent.prev_y and agent.prev_x:
                    min_dist_prev_pos_goal = min([self.dist_pos_goal(prev_pos, goal) for goal in goals])
                    if min_dist_pos_goal < min_dist_prev_pos_goal and min_dist_pos_goal != 0:
                        if self.reward_type == RewardType.GLOBAL:
                            rewards += 1 / min_dist_pos_goal
                        elif self.reward_type == RewardType.INDIVIDUAL:
                            agent_id = self.grid[_LAYER_AGENTS, agent.y, agent.x]
                            rewards[agent_id - 1] += 1 / min_dist_pos_goal
                        elif self.reward_type == RewardType.TWO_STAGE:
                            agent_id = self.grid[_LAYER_AGENTS, agent.y, agent.x]
                            self.agents[agent_id - 1].has_delivered = True          
                            rewards[agent_id - 1] += 0.5 / min_dist_pos_goal
            elif not agent.carrying_shelf:
                min_dist_pos_request = min([self.dist_pos_goal(pos, shelf) for shelf in self.requested_shelf_coordinates])
                if agent.prev_y and agent.prev_x:
                    min_dist_prev_pos_request = min([self.dist_pos_goal(prev_pos, shelf) for shelf in self.requested_shelf_coordinates])
                    if min_dist_pos_request < min_dist_prev_pos_request and min_dist_pos_request != 0:
                        if self.reward_type == RewardType.GLOBAL:
                            rewards += 1 / min_dist_pos_request
                        elif self.reward_type == RewardType.INDIVIDUAL:
                            agent_id = self.grid[_LAYER_AGENTS, agent.y, agent.x]
                            rewards[agent_id - 1] += 1 / min_dist_pos_request
                        elif self.reward_type == RewardType.TWO_STAGE:
                            agent_id = self.grid[_LAYER_AGENTS, agent.y, agent.x]
                            self.agents[agent_id - 1].has_delivered = True          
                            rewards[agent_id - 1] += 0.5 / min_dist_pos_request
        '''

        # print("rewards:", rewards)

        if shelf_delivered:
            self._cur_inactive_steps = 0
        else:
            self._cur_inactive_steps += 1
        self._cur_steps += 1

        if (
            self.max_inactivity_steps
            and self._cur_inactive_steps >= self.max_inactivity_steps
        ) or (self.max_steps and self._cur_steps >= self.max_steps):
            dones = self.n_agents * [True]
        else:
            dones = self.n_agents * [False]

        new_obs = tuple([self._make_obs(agent) for agent in self.agents])
        info = {}
        return new_obs, list(rewards), dones, info

    def render(self, mode="human"):
        if not self.renderer:
            from robotic_warehouse.rendering import Viewer

            self.renderer = Viewer(self.grid_size)
        return self.renderer.render(self, return_rgb_array=mode == "rgb_array")

    def close(self):
        if self.renderer:
            self.renderer.close()

    def seed(self, seed=None):
        ...


if __name__ == "__main__":
    env = Warehouse(9, 8, 3, 10, 3, 1, 5, None, None, RewardType.GLOBAL)
    # env = Warehouse(1, 3, 3, 2, 3, 1, 5, None, None, RewardType.INDIVIDUAL)
    env.reset()
    import time
    from tqdm import tqdm

    time.sleep(2)
    # env.render()
    # env.step(18 * [Action.LOAD] + 2 * [Action.NOOP])

    for _ in tqdm(range(1000000)):
        time.sleep(2)
        env.render()
        actions = env.action_space.sample()
        env.step(actions)
