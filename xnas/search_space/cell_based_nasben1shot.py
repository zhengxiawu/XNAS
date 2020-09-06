import copy
import sys
sys.path.append('.')
import itertools
import random
from abc import abstractmethod

import ConfigSpace
import numpy as np
from nasbench import api
from collections import namedtuple
import torch.nn.functional as F
from torch.autograd import Variable
import torch.nn as nn
import torch

from xnas.core.utils import index_to_one_hot, one_hot_to_index

INPUT = 'input'
OUTPUT = 'output'
CONV1X1 = 'conv1x1-bn-relu'
CONV3X3 = 'conv3x3-bn-relu'
MAXPOOL3X3 = 'maxpool3x3'
OUTPUT_NODE = 6

PRIMITIVES = [
    'maxpool3x3',
    'conv3x3-bn-relu',
    'conv1x1-bn-relu'
]

OPS = {
    # For nasbench
    'maxpool3x3': lambda C, stride, affine: nn.MaxPool2d(3, stride=stride, padding=1),
    'conv3x3-bn-relu': lambda C, stride, affine: Conv3x3BnRelu(C, stride),
    'conv1x1-bn-relu': lambda C, stride, affine: Conv1x1BnRelu(C, stride),
}

# base operations
class ConvBnRelu(nn.Module):

    def __init__(self, C_in, C_out, kernel_size, stride, padding=1):
        super(ConvBnRelu, self).__init__()
        self.op = nn.Sequential(
            nn.Conv2d(C_in, C_out, kernel_size, stride=stride, padding=padding, bias=False),
            nn.BatchNorm2d(C_out, affine=True, momentum=BN_MOMENTUM, eps=BN_EPSILON),
            nn.ReLU(inplace=False)
        )

    def forward(self, x):
        return self.op(x)


class Conv3x3BnRelu(nn.Module):

    def __init__(self, channels, stride):
        super(Conv3x3BnRelu, self).__init__()
        self.op = ConvBnRelu(C_in=channels, C_out=channels, kernel_size=3, stride=stride)

    def forward(self, x):
        return self.op(x)

class Conv1x1BnRelu(nn.Module):

    def __init__(self, channels, stride):
        super(Conv1x1BnRelu, self).__init__()
        self.op = ConvBnRelu(C_in=channels, C_out=channels, kernel_size=1, stride=stride, padding=0)

    def forward(self, x):
        return self.op(x)

class ReLUConvBN(nn.Module):

    def __init__(self, C_in, C_out, kernel_size, stride, padding, affine=True):
        super(ReLUConvBN, self).__init__()
        self.op = nn.Sequential(
            nn.ReLU(inplace=False),
            nn.Conv2d(C_in, C_out, kernel_size, stride=stride, padding=padding, bias=False),
            nn.BatchNorm2d(C_out, affine=affine)
        )

    def forward(self, x):
        return self.op(x)

class FactorizedReduce(nn.Module):

    def __init__(self, C_in, C_out, affine=True):
        super(FactorizedReduce, self).__init__()
        assert C_out % 2 == 0
        self.relu = nn.ReLU(inplace=False)
        self.conv_1 = nn.Conv2d(C_in, C_out // 2, 1, stride=2, padding=0, bias=False)
        self.conv_2 = nn.Conv2d(C_in, C_out // 2, 1, stride=2, padding=0, bias=False)
        self.bn = nn.BatchNorm2d(C_out, affine=affine)

    def forward(self, x):
        x = self.relu(x)
        out = torch.cat([self.conv_1(x), self.conv_2(x[:, :, 1:, 1:])], dim=1)
        out = self.bn(out)
        return out

# Batch Normalization 
BN_MOMENTUM = 0.997
BN_EPSILON = 1e-5

# Some utils
def upscale_to_nasbench_format(adjacency_matrix):

    return np.insert(
        np.insert(adjacency_matrix,
                  5, [0, 0, 0, 0, 0, 0], axis=1),
        5, [0, 0, 0, 0, 0, 0, 0], axis=0)

def nasbench_forma_to_small(adjacency_matrix):
    return np.delete(
        np.delete(adjacency_matrix ,5, axis = 0), 5, axis=1)

def get_weights_from_arch(arch, intermediate_nodes, search_space):
    adjacency_matrix, node_list = arch
    
    if search_space != 3:
        adjacency_matrix=nasbench_forma_to_small(adjacency_matrix)

    num_ops = len(PRIMITIVES)

    # Assign the sampled ops to the mixed op weights.
    # These are not optimized
    alphas_mixed_op = Variable(torch.zeros(intermediate_nodes, num_ops).cuda(), requires_grad=False)
    for idx, op in enumerate(node_list):
        alphas_mixed_op[idx][PRIMITIVES.index(op)] = 1

    # Set the output weights
    alphas_output = Variable(torch.zeros(1, intermediate_nodes + 1).cuda(), requires_grad=False)
    for idx, label in enumerate(list(adjacency_matrix[:, -1][:-1])):
        alphas_output[0][idx] = label

    # Initialize the weights for the inputs to each choice block.
    if search_space == 1:
        begin = 3
    else:
        begin = 2
    alphas_inputs = [Variable(torch.zeros(1, n_inputs).cuda(), requires_grad=False) for n_inputs in
                     range(begin, intermediate_nodes + 1)]
    for alpha_input in alphas_inputs:
        connectivity_pattern = list(adjacency_matrix[:alpha_input.shape[1], alpha_input.shape[1]])
        for idx, label in enumerate(connectivity_pattern):
            alpha_input[0][idx] = label

    # Total architecture parameters
    arch_parameters = [
        alphas_mixed_op,
        alphas_output,
        *alphas_inputs
    ]
    return arch_parameters



def parent_combinations_old(adjacency_matrix, node, n_parents=2):
    """Get all possible parent combinations for the current node."""

    if node != 1:
        # Parents can only be nodes which have an index that is lower than the current index,
        # because of the upper triangular adjacency matrix and because the index is also a
        # topological ordering in our case.
        return itertools.combinations(np.argwhere(adjacency_matrix[:node, node] == 0).flatten(),
                                      n_parents)  # (e.g. (0, 1), (0, 2), (1, 2), ...
    else:
        return [[0]]


def parent_combinations(node, num_parents):
    if node == 1 and num_parents == 1:
        return [(0,)]
    else:
        return list(itertools.combinations(list(range(int(node))), num_parents))


class SearchSpace:

    def __init__(self, search_space_number, num_intermediate_nodes):
        self.search_space_number = search_space_number
        self.num_intermediate_nodes = num_intermediate_nodes
        self.num_parents_per_node = {}

        self.run_history = []

    @abstractmethod
    def create_nasbench_adjacency_matrix(self, parents, **kwargs):

        pass

    def sample(self, with_loose_ends, upscale=True):

        if with_loose_ends:
            adjacency_matrix_sample = self._sample_adjacency_matrix_with_loose_ends()
        else:
            adjacency_matrix_sample = self._sample_adjacency_matrix_without_loose_ends(
                adjacency_matrix=np.zeros([self.num_intermediate_nodes + 2, self.num_intermediate_nodes + 2]),
                node=self.num_intermediate_nodes + 1)
            assert self._check_validity_of_adjacency_matrix(adjacency_matrix_sample), 'Incorrect graph'

        if upscale and self.search_space_number in [1, 2]:
            adjacency_matrix_sample = upscale_to_nasbench_format(adjacency_matrix_sample)
        return adjacency_matrix_sample, random.choices(PRIMITIVES, k=self.num_intermediate_nodes)

    def _sample_adjacency_matrix_with_loose_ends(self):

        parents_per_node = [random.sample(list(itertools.combinations(list(range(int(node))), num_parents)), 1) for
                            node, num_parents in self.num_parents_per_node.items()][2:] 
        parents = {
            '0': [],
            '1': [0]
        }
        for node, node_parent in enumerate(parents_per_node, 2):
            parents[str(node)] = node_parent
        adjacency_matrix = self._create_adjacency_matrix_with_loose_ends(parents)
        return adjacency_matrix

    def _sample_adjacency_matrix_without_loose_ends(self, adjacency_matrix, node):
       
        req_num_parents = self.num_parents_per_node[str(node)]
        current_num_parents = np.sum(adjacency_matrix[:, node], dtype=np.int)
        num_parents_left = req_num_parents - current_num_parents
        sampled_parents = \
            random.sample(list(parent_combinations_old(adjacency_matrix, node, n_parents=num_parents_left)), 1)[0]
        for parent in sampled_parents:
            adjacency_matrix[parent, node] = 1
            adjacency_matrix = self._sample_adjacency_matrix_without_loose_ends(adjacency_matrix, parent)
        return adjacency_matrix

    @abstractmethod
    def generate_adjacency_matrix_without_loose_ends(self, **kwargs):
        """Returns every adjacency matrix in the search space without loose ends."""
        pass

    def convert_config_to_nasbench_format(self, config):
  
        parents = {node: config["choice_block_{}_parents".format(node)] for node in
                   list(self.num_parents_per_node.keys())[1:]} 
        parents['0'] = []
        adjacency_matrix = self.create_nasbench_adjacency_matrix_with_loose_ends(parents)
        ops = [config["choice_block_{}_op".format(node)] for node in list(self.num_parents_per_node.keys())[1:-1]]
        return adjacency_matrix, ops

    def get_configuration_space(self):
        cs = ConfigSpace.ConfigurationSpace()

        for node in list(self.num_parents_per_node.keys())[1:-1]:
            cs.add_hyperparameter(ConfigSpace.CategoricalHyperparameter("choice_block_{}_op".format(node),
                                                                        [CONV1X1, CONV3X3, MAXPOOL3X3])) 

        for choice_block_index, num_parents in list(self.num_parents_per_node.items())[1:]:
            cs.add_hyperparameter(
                ConfigSpace.CategoricalHyperparameter(
                    "choice_block_{}_parents".format(choice_block_index),
                    parent_combinations(node=choice_block_index, num_parents=num_parents))) 
        return cs 

    def generate_search_space_without_loose_ends(self):
        # Create all possible connectivity patterns
        for iter, adjacency_matrix in enumerate(self.generate_adjacency_matrix_without_loose_ends()):
            print(iter)
            # Print graph
            # Evaluate every possible combination of node ops.
            n_repeats = int(np.sum(np.sum(adjacency_matrix, axis=1)[1:-1] > 0)) # 
            for combination in itertools.product([CONV1X1, CONV3X3, MAXPOOL3X3], repeat=n_repeats):
                # Create node labels
                # Add some op as node 6 which isn't used, here conv1x1
                ops = [INPUT]
                combination = list(combination)
                for i in range(5):
                    if np.sum(adjacency_matrix, axis=1)[i + 1] > 0:
                        ops.append(combination.pop())
                    else:
                        ops.append(CONV1X1) 
                assert len(combination) == 0, 'Something is wrong'
                ops.append(OUTPUT)

                # Create nested list from numpy matrix
                nasbench_adjacency_matrix = adjacency_matrix.astype(np.int).tolist()

                # Assemble the model spec
                model_spec = api.ModelSpec(
                    # Adjacency matrix of the module
                    matrix=nasbench_adjacency_matrix,
                    # Operations at the vertices of the module, matches order of matrix
                    ops=ops)

                yield adjacency_matrix, ops, model_spec

    def _generate_adjacency_matrix(self, adjacency_matrix, node):
     
        if self._check_validity_of_adjacency_matrix(adjacency_matrix):
            # If graph from search space then yield.
            yield adjacency_matrix 
        else:
            req_num_parents = self.num_parents_per_node[str(node)]
            current_num_parents = np.sum(adjacency_matrix[:, node], dtype=np.int)
            num_parents_left = req_num_parents - current_num_parents

            for parents in parent_combinations_old(adjacency_matrix, node, n_parents=num_parents_left):
                # Make copy of adjacency matrix so that when it returns to this stack
                # it can continue with the unmodified adjacency matrix
                adjacency_matrix_copy = copy.copy(adjacency_matrix)
                for parent in parents:
                    adjacency_matrix_copy[parent, node] = 1
                    for graph in self._generate_adjacency_matrix(adjacency_matrix=adjacency_matrix_copy, node=parent):
                        yield graph

    def _create_adjacency_matrix(self, parents, adjacency_matrix, node):

        if self._check_validity_of_adjacency_matrix(adjacency_matrix):
            # If graph from search space then yield.
            return adjacency_matrix
        else:
            for parent in parents[str(node)]:
                adjacency_matrix[parent, node] = 1
                if parent != 0:
                    adjacency_matrix = self._create_adjacency_matrix(parents=parents, adjacency_matrix=adjacency_matrix,
                                                                     node=parent)
            return adjacency_matrix

    def _create_adjacency_matrix_with_loose_ends(self, parents):
        # Create the adjacency_matrix on a per node basis
    
        adjacency_matrix = np.zeros([len(parents), len(parents)])
        for node, node_parents in parents.items():
            for parent in node_parents:
                adjacency_matrix[parent, int(node)] = 1
        return adjacency_matrix

    def _check_validity_of_adjacency_matrix(self, adjacency_matrix):

        # Check that the graph contains nodes
        num_intermediate_nodes = sum(np.array(np.sum(adjacency_matrix, axis=1) > 0, dtype=int)[1:-1])
        if num_intermediate_nodes == 0:
            return False

        # Check that every node has exactly the right number of inputs
        col_sums = np.sum(adjacency_matrix[:, :], axis=0)
        for col_idx, col_sum in enumerate(col_sums):
            if col_sum > 0:
                if col_sum != self.num_parents_per_node[str(col_idx)]:
                    return False

        # Check that if a node has outputs then it should also have incoming edges (apart from zero)
        col_sums = np.sum(np.sum(adjacency_matrix, axis=0) > 0)
        row_sums = np.sum(np.sum(adjacency_matrix, axis=1) > 0)
        if col_sums != row_sums:
            return False

        # Check that the input node is always connected. Otherwise the graph is disconnected.
        row_sum = np.sum(adjacency_matrix, axis=1)
        if row_sum[0] == 0:
            return False

        # Check that the graph returned has no more than 9 edges.
        num_edges = np.sum(adjacency_matrix.flatten())
        if num_edges > 9:
            return False

        return True

Architecture = namedtuple('Architecture', ['adjacency_matrix', 'node_list'])

class Model(object):

    def __init__(self):
        self.arch = None
        self.validation_accuracy = None
        self.test_accuracy = None
        self.training_time = None
        self.budget = None

    def update_data(self, arch, nasbench_data, budget):
        self.arch = arch
        self.validation_accuracy = nasbench_data['validation_accuracy']
        self.test_accuracy = nasbench_data['test_accuracy']
        self.training_time = nasbench_data['training_time']
        self.budget = budget

    def query_nasbench(self, nasbench, sample, search_space=None):
        config = ConfigSpace.Configuration(
            search_space.get_configuration_space(), vector=sample
        ) 
        adjacency_matrix, node_list = search_space.convert_config_to_nasbench_format(config)
        if type(search_space) == SearchSpace3:
            node_list = [INPUT, *node_list, OUTPUT]
        else:
            node_list = [INPUT, *node_list, CONV1X1, OUTPUT]
        adjacency_list = adjacency_matrix.astype(np.int).tolist()
        model_spec = api.ModelSpec(matrix=adjacency_list, ops=node_list)

        nasbench_data = nasbench.query(model_spec)
        self.arch = Architecture(adjacency_matrix=adjacency_matrix,
                                 node_list=node_list)
        self.validation_accuracy = nasbench_data['validation_accuracy']
        self.test_accuracy = nasbench_data['test_accuracy']
        self.training_time = nasbench_data['training_time']

class SearchSpace1(SearchSpace):
    def __init__(self):
        super(SearchSpace1, self).__init__(search_space_number=1, num_intermediate_nodes=4)
        """
        SEARCH SPACE 1
        """
        self.num_parents_per_node = {
            '0': 0,
            '1': 1,
            '2': 2,
            '3': 2,
            '4': 2,
            '5': 2
        }
        if sum(self.num_parents_per_node.values()) > 9:
            raise ValueError('Each nasbench cell has at most 9 edges.')

        self.test_min_error = 0.05448716878890991
        self.valid_min_error = 0.049278855323791504

    def create_nasbench_adjacency_matrix(self, parents, **kwargs):

        adjacency_matrix = self._create_adjacency_matrix(parents, adjacency_matrix=np.zeros([6, 6]),
                                                         node=OUTPUT_NODE - 1)
        # Create nasbench compatible adjacency matrix
        return upscale_to_nasbench_format(adjacency_matrix)

    def create_nasbench_adjacency_matrix_with_loose_ends(self, parents):

        return upscale_to_nasbench_format(self._create_adjacency_matrix_with_loose_ends(parents))

    def generate_adjacency_matrix_without_loose_ends(self):

        for adjacency_matrix in self._generate_adjacency_matrix(adjacency_matrix=np.zeros([6, 6]),
                                                                node=OUTPUT_NODE - 1):
            yield upscale_to_nasbench_format(adjacency_matrix)

    def objective_function(self, nasbench, config, budget=108):

        adjacency_matrix, node_list = super(SearchSpace1, self).convert_config_to_nasbench_format(config)
        # adjacency_matrix = upscale_to_nasbench_format(adjacency_matrix)
        node_list = [INPUT, *node_list, CONV1X1, OUTPUT]
        adjacency_list = adjacency_matrix.astype(np.int).tolist()
        model_spec = api.ModelSpec(matrix=adjacency_list, ops=node_list)
        nasbench_data = nasbench.query(model_spec, epochs=budget)
        return nasbench_data['validation_accuracy'], nasbench_data['training_time']

class SearchSpace2(SearchSpace):
    def __init__(self):
        self.search_space_number = 2
        self.num_intermediate_nodes = 4
        super(SearchSpace2, self).__init__(search_space_number=self.search_space_number,
                                           num_intermediate_nodes=self.num_intermediate_nodes)
        """
        SEARCH SPACE 2
        """
        self.num_parents_per_node = {
            '0': 0,
            '1': 1,
            '2': 1,
            '3': 2,
            '4': 2,
            '5': 3
        }
        if sum(self.num_parents_per_node.values()) > 9:
            raise ValueError('Each nasbench cell has at most 9 edges.')

        self.test_min_error = 0.057592153549194336
        self.valid_min_error = 0.051582515239715576

    def create_nasbench_adjacency_matrix(self, parents, **kwargs):
        adjacency_matrix = self._create_adjacency_matrix(parents, adjacency_matrix=np.zeros([6, 6]),
                                                         node=OUTPUT_NODE - 1)
        # Create nasbench compatible adjacency matrix
        return upscale_to_nasbench_format(adjacency_matrix)

    def create_nasbench_adjacency_matrix_with_loose_ends(self, parents):
        return upscale_to_nasbench_format(self._create_adjacency_matrix_with_loose_ends(parents))

    def generate_adjacency_matrix_without_loose_ends(self):
        for adjacency_matrix in self._generate_adjacency_matrix(adjacency_matrix=np.zeros([6, 6]),
                                                                node=OUTPUT_NODE - 1):
            yield upscale_to_nasbench_format(adjacency_matrix)

    def objective_function(self, nasbench, config, budget=108):
        adjacency_matrix, node_list = super(SearchSpace2, self).convert_config_to_nasbench_format(config)
        # adjacency_matrix = upscale_to_nasbench_format(adjacency_matrix)
        node_list = [INPUT, *node_list, CONV1X1, OUTPUT]
        adjacency_list = adjacency_matrix.astype(np.int).tolist()
        model_spec = api.ModelSpec(matrix=adjacency_list, ops=node_list)
        nasbench_data = nasbench.query(model_spec, epochs=budget)

        # record the data to history
        architecture = Model()
        arch = Architecture(adjacency_matrix=adjacency_matrix,
                            node_list=node_list)
        architecture.update_data(arch, nasbench_data, budget)
        self.run_history.append(architecture)

        return nasbench_data['validation_accuracy'], nasbench_data['training_time']


class SearchSpace3(SearchSpace):
    def __init__(self):
        self.search_space_number = 3
        self.num_intermediate_nodes = 5
        super(SearchSpace3, self).__init__(search_space_number=self.search_space_number,
                                           num_intermediate_nodes=self.num_intermediate_nodes)
        """
        SEARCH SPACE 3
        """
        self.num_parents_per_node = {
            '0': 0,
            '1': 1,
            '2': 1,
            '3': 1,
            '4': 2,
            '5': 2,
            '6': 2
        }
        if sum(self.num_parents_per_node.values()) > 9:
            raise ValueError('Each nasbench cell has at most 9 edges.')

        self.test_min_error = 0.05338543653488159
        self.valid_min_error = 0.04847759008407593

    def create_nasbench_adjacency_matrix(self, parents, **kwargs):
        # Create nasbench compatible adjacency matrix
        adjacency_matrix = self._create_adjacency_matrix(parents, adjacency_matrix=np.zeros([7, 7]), node=OUTPUT_NODE)
        return adjacency_matrix

    def create_nasbench_adjacency_matrix_with_loose_ends(self, parents):
        return self._create_adjacency_matrix_with_loose_ends(parents)

    def generate_adjacency_matrix_without_loose_ends(self):
        for adjacency_matrix in self._generate_adjacency_matrix(adjacency_matrix=np.zeros([7, 7]), node=OUTPUT_NODE):
            yield adjacency_matrix
            
    def objective_function(self, nasbench, config, budget=108):
        adjacency_matrix, node_list = super(SearchSpace3, self).convert_config_to_nasbench_format(config)
        node_list = [INPUT, *node_list, OUTPUT]
        adjacency_list = adjacency_matrix.astype(np.int).tolist()
        model_spec = api.ModelSpec(matrix=adjacency_list, ops=node_list)
        nasbench_data = nasbench.query(model_spec, epochs=budget)
        return nasbench_data['validation_accuracy'], nasbench_data['training_time']

class MixedOp(nn.Module):

    def __init__(self, C, stride):
        super(MixedOp, self).__init__()
        self._ops = nn.ModuleList()
        for primitive in PRIMITIVES:
            op = OPS[primitive](C, stride, False)
            self._ops.append(op)

    def forward(self, x, weights):
        return sum(w * op(x) for w, op in zip(weights, self._ops))

class ChoiceBlock(nn.Module):

    def __init__(self, C_in):
        super(ChoiceBlock, self).__init__()
        self.mixed_op = MixedOp(C_in, stride=1)

    def forward(self, inputs, input_weights, weights):
        if input_weights is not None:
            inputs = [w * t for w, t in zip(input_weights.squeeze(0), inputs)]

        input_to_mixed_op = sum(inputs)

        # Apply Mixed Op
        output = self.mixed_op(input_to_mixed_op, weights=weights)
        return output

class Cell(nn.Module):

    def __init__(self, steps, C_prev, C, layer, search_space):
        super(Cell, self).__init__()
        self._steps = steps

        self._choice_blocks = nn.ModuleList()
        self._bns = nn.ModuleList()
        self.search_space = search_space

        self._input_projections = nn.ModuleList()
        C_in = C_prev if layer == 0 else C_prev * steps

        for i in range(self._steps):
            choice_block = ChoiceBlock(C_in=C)
            self._choice_blocks.append(choice_block)
            self._input_projections.append(ConvBnRelu(C_in=C_in, C_out=C, kernel_size=1, stride=1, padding=0))

        self._input_projections.append(ConvBnRelu(C_in=C_in, C_out=C * self._steps, kernel_size=1, stride=1, padding=0))

    def forward(self, s0, weights, output_weights, input_weights):

        states = []

        # Loop through the choice blocks of each cell
        for choice_block_idx in range(self._steps):
            if input_weights is not None:
                if (choice_block_idx == 0) or (choice_block_idx == 1 and type(self.search_space) == SearchSpace1):
                    input_weight = None
                else:
                    input_weight = input_weights.pop(0)

            s = self._choice_blocks[choice_block_idx](inputs=[self._input_projections[choice_block_idx](s0), *states],
                                                      input_weights=input_weight, weights=weights[choice_block_idx])
            states.append(s)
        input_to_output_edge = self._input_projections[-1](s0)
        assert (len(input_weights) == 0, 'Something went wrong here.')

        if output_weights is None:
            tensor_list = states
        else:
            tensor_list = [w * t for w, t in zip(output_weights[0][1:], states)]

        return output_weights[0][0] * input_to_output_edge + torch.cat(tensor_list, dim=1)

class Network(nn.Module):

    def __init__(self, C, num_classes, layers, search_space, steps=4):
        super(Network, self).__init__()
        self._C = C 
        self._num_classes = num_classes 
        self._layers = layers 
        self._steps = steps 
        self.search_space = search_space 

        # In NASBench the stem has 128 output channels
        C_curr = C
        self.stem = ConvBnRelu(C_in=3, C_out=C_curr, kernel_size=3, stride=1)

        self.cells = nn.ModuleList()
        C_prev = C_curr
        for i in range(layers):
            if i in [layers // 3, 2 * layers // 3]:
                # Double the number of channels after each down-sampling step
                # Down-sample in forward method
                C_curr *= 2
            cell = Cell(steps=self._steps, C_prev=C_prev, C=C_curr, layer=i, search_space=search_space) 
            self.cells += [cell]
            C_prev = C_curr
        self.postprocess = ReLUConvBN(C_in=C_prev * self._steps, C_out=C_curr, kernel_size=1, stride=1, padding=0,
                                      affine=False)

        self.classifier = nn.Linear(C_prev, num_classes)

    def forward(self, input, sample):
        # get weights by sample
        sample_index = one_hot_to_index(np.array(sample))
        config = ConfigSpace.Configuration(self.search_space.get_configuration_space(), vector = sample_index)
        adjacency_matrix, node_list = self.search_space.convert_config_to_nasbench_format(config)
        arch_parameters = get_weights_from_arch((adjacency_matrix, node_list), self._steps, self.search_space.search_space_number)
        # NASBench only has one input to each cell
        s0 = self.stem(input)
        for i, cell in enumerate(self.cells):
            if i in [self._layers // 3, 2 * self._layers // 3]:
                # Perform down-sampling by factor 1/2
                s0 = nn.MaxPool2d(kernel_size=2, stride=2, padding=1)(s0)

            # get mixed_op_weights
            mixed_op_weights = arch_parameters[0]

            # get output_weights
            output_weights = arch_parameters[1] 

            # get input_weights
            input_weights = [alpha for alpha in arch_parameters[2:]]

            s0 = cell(s0, mixed_op_weights, output_weights, input_weights)

        # Include one more preprocessing step here
        s0 = self.postprocess(s0)  # [N, C_max * (steps + 1), w, h] -> [N, C_max, w, h]

        # Global Average Pooling by averaging over last two remaining spatial dimensions
        out = s0.view(*s0.shape[:2], -1).mean(-1)
        logits = self.classifier(out.view(out.size(0), -1))
        return logits
    def genotype(self, theta):
        sample = np.argmax(theta, axis=1)
        config = ConfigSpace.Configuration(self.search_space.get_configuration_space(), vector = sample)
        adjacency_matrix, node_list = self.search_space.convert_config_to_nasbench_format(config)
        if self.search_space.search_space_number == 3:
            node_list = [INPUT, *node_list, OUTPUT]
        else:
            node_list = [INPUT, *node_list, CONV1X1, OUTPUT]
        result = "adjacency_matrix:"+str(adjacency_matrix)+"node_list:"+str(node_list)
        return result

#bulid API
def _NASbench1shot1_1():
    from xnas.core.config import cfg
    return Network(C = cfg.SPACE.CHANNEL,
                   num_classes = cfg.SPACE.NUM_CLASSES,
                   layers = 9,
                   search_space = SearchSpace1())

def _NASbench1shot1_2():
    from xnas.core.config import cfg
    return Network(C = cfg.SPACE.CHANNEL,
                   num_classes = cfg.SPACE.NUM_CLASSES,
                   layers = 9,
                   search_space = SearchSpace2())

def _NASbench1shot1_3():
    from xnas.core.config import cfg
    return Network(C = cfg.SPACE.CHANNEL,
                   num_classes = cfg.SPACE.NUM_CLASSES,
                   layers = 9,
                   search_space = SearchSpace3())