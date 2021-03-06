#!/usr/bin/env python

from QUBEKit.decorators import for_all_methods, timer_logger
from QUBEKit.helpers import check_net_charge

from os.path import exists
from collections import OrderedDict
from numpy import array, cross, dot, sqrt


@for_all_methods(timer_logger)
class LennardJones:

    def __init__(self, molecule, config_dict):

        self.molecule = molecule
        self.defaults_dict, self.qm, self.fitting, self.descriptions = config_dict

        # self.ddec_data is the DDEC molecule data in the format:
        # ['atom number', 'atom type', 'x', 'y', 'z', 'charge', 'x dipole', 'y dipole', 'z dipole', 'vol']
        # It will be extended and tweaked by each core method of this class.

        self.ddec_data = []

        # Sigma: Angs -> nm
        self.sigma_conversion = 0.1

        # Epsilon: (Ha * (Bohr ** 6)) / (Angs ** 6) -> kJ / mol
        # (Ha * (Bohr ** 6)) / (Angs ** 6) -> Ha = * 0.529177 ** 6
        # Ha -> kcal / mol = * 627.509
        # kcal / mol -> kJ / mol = * 4.184
        # PI = 57.65240039
        self.epsilon_conversion = 57.65240039

        self.non_bonded_force = {}

    def extract_params_chargemol(self):
        """
        From Chargemol output files, extract the necessary parameters for calculation of L-J.
        Desired format:
        ['atom number', 'atom type', 'x', 'y', 'z', 'charge', 'x_dipole', 'y_dipole', 'z_dipole', 'vol']
        All vals are float except atom number (int) and atom type (str).
        """

        if self.qm['ddec_version'] == 6:
            net_charge_file_name = 'DDEC6_even_tempered_net_atomic_charges.xyz'

        elif self.qm['ddec_version'] == 3:
            net_charge_file_name = 'DDEC3_net_atomic_charges.xyz'

        else:
            raise ValueError('Unsupported DDEC version; please use version 3 or 6.')

        if not exists(net_charge_file_name):
            raise FileNotFoundError('\nCannot find the DDEC output file.\nThis could be indicative of several issues.\n'
                                    'Please check Chargemol is installed in the correct location and that the configs'
                                    ' point to that location.')

        with open(net_charge_file_name, 'r+') as charge_file:

            lines = charge_file.readlines()

        # Find number of atoms
        atom_total = int(lines[0])

        for pos, row in enumerate(lines):
            # Data marker:
            if 'The following XYZ' in row:
                start_pos = pos + 2
                break
        else:
            raise EOFError(f'Cannot find charge data in {net_charge_file_name}.')

        # Append the atom number and type, coords, charge, dipoles:
        for line in lines[start_pos: start_pos + atom_total]:
            a_number, a_type, *data = line.split()
            self.ddec_data.append([int(a_number), a_type] + [float(datum) for datum in data])

        charges = [atom[5] for atom in self.ddec_data]
        check_net_charge(charges, ideal_net=self.defaults_dict['charge'])

        r_cubed_file_name = 'DDEC_atomic_Rcubed_moments.xyz'

        with open(r_cubed_file_name, 'r+') as vol_file:

            lines = vol_file.readlines()

        vols = [float(line.split()[-1]) for line in lines[2:atom_total + 2]]

        for pos, atom in enumerate(self.ddec_data):
            atom.append(vols[pos])

    def extract_params_onetep(self):
        """
        From ONETEP output files, extract the necessary parameters for calculation of L-J.
        Desired format:
        ['atom number', 'atom type', 'x', 'y', 'z', 'charge', 'vol']
        All vals are float except atom number (int) and atom type (str).
        """

        # We know this from the molecule object self.molecule try to get the info from there
        for pos, atom in enumerate(self.molecule.molecule['input']):
            self.ddec_data.append([pos + 1] + [atom[i] for i in range(4)])

        # Second file contains the rest (charges, dipoles and volumes):
        with open('ddec.onetep', 'r') as file:
            lines = file.readlines()

        charge_pos, vol_pos = False, False
        for pos, line in enumerate(lines):

            # Charges marker in file:
            if 'DDEC density' in line:
                charge_pos = pos + 7

            # Volumes marker in file:
            if 'DDEC Radial' in line:
                vol_pos = pos + 4

        if not (charge_pos and vol_pos):
            raise EOFError('Cannot locate charges and / or volumes in ddec.onetep file.')
            
        charges = [float(line.split()[-1]) for line in lines[charge_pos: charge_pos + len(self.ddec_data)]]
        check_net_charge(charges, ideal_net=self.defaults_dict['charge'])

        # Add the AIM-Valence and the AIM-Core to get V^AIM
        volumes = [float(line.split()[2]) + float(line.split()[3]) for line in lines[vol_pos: vol_pos + len(self.ddec_data)]]

        # Add the charges and volumes to the end of the inner lists (containing coords etc)
        for pos, atom in enumerate(self.ddec_data):
            atom.extend((charges[pos], volumes[pos]))

    def append_ais_bis(self):
        """
        Use the AIM parameters from extract_params_*() to calculate a_i and b_i according to paper.
        Calculations from paper have been combined and simplified for faster computation.
        """

        # Beware weird units, (wrong in the paper too).
        # 'elem' : [vfree, bfree, rfree]
        # Units: [vfree: Bohr ** 3, bfree: Ha * (Bohr ** 6), rfree: Angs]

        elem_dict = {
            'H': [7.6, 6.5, 1.64],
            'C': [34.4, 46.6, 2.08],
            'N': [25.9, 24.2, 1.72],
            'O': [22.1, 15.6, 1.60],
            'F': [18.2, 9.5, 1.58],
            'S': [75.2, 134.0, 2.00],
            'Cl': [65.1, 94.6, 1.88],
            'Br': [95.7, 162.0, 1.96],
        }

        for pos, atom in enumerate(self.ddec_data):

            # r_aim = r_free * ((vol / v_free) ** (1 / 3))
            r_aim = elem_dict[f'{atom[1]}'][2] * ((atom[-1] / elem_dict[f'{atom[1]}'][0]) ** (1 / 3))

            # b_i = bfree * ((vol / v_free) ** 2)
            b_i = elem_dict[f'{atom[1]}'][1] * ((atom[-1] / elem_dict[f'{atom[1]}'][0]) ** 2)

            a_i = 32 * b_i * (r_aim ** 6)

            self.ddec_data[pos] += [r_aim, b_i, a_i]

    def calculate_sig_eps(self):
        """
        Adds the sigma, epsilon terms to the ligand class object as a dictionary.
        The ligand class object (NonbondedForce) is stored as an empty dictionary until this method is called.
        first_pass argument prevents the sigmas being recalculated (unlike the epsilons).
        """

        # Creates Nonbondedforce dict for later xml creation.
        # Format: {0: [charge, sigma, epsilon], 1: [charge, sigma, epsilon], ... }
        # This follows the usual ordering of the atoms such as in molecule.molecule.

        for pos, atom in enumerate(self.ddec_data):

            if atom[-1] == 0:
                sigma = epsilon = 0

            else:
                # sigma = (a_i / b_i) ** (1 / 6)
                sigma = (atom[-1] / atom[-2]) ** (1 / 6)
                sigma *= self.sigma_conversion

                # epsilon = (b_i ** 2) / (4 * a_i)
                epsilon = (atom[-2] ** 2) / (4 * atom[-1])
                epsilon *= self.epsilon_conversion

            self.non_bonded_force[pos] = [str(atom[5]), str(sigma), str(epsilon)]

    def correct_polar_hydrogens(self):
        """
        Identifies the polar Hydrogens and changes the a_i, b_i values accordingly.
        May be removed / heavily changed if we switch away from atom typing and use SMARTS.
        """

        # Create dictionary which stores the atom number and its type:
        # atoms = {1: 'C', 2: 'C', 3: 'H', 4: 'H', ...}
        # (+1 because topology indices count from 1, not 0)
        positions = {self.molecule.molecule['input'].index(atom) + 1: atom[0] for atom in self.molecule.molecule['input']}

        # Loop through pairs in topology
        # Create new pair list with atom types and positions using the dictionary:
        # new_pairs = [('1C', '3H'), ('1C', '4H'), ('1C', '5H') ...]
        new_pairs = []
        for pair in self.molecule.topology.edges:
            new_pair = (str(pair[0]) + positions[pair[0]], str(pair[1]) + positions[pair[1]])
            new_pairs.append(new_pair)

        # Find all the polar hydrogens and store their positions / atom numbers
        polars = []
        for pair in new_pairs:
            if 'O' in pair[0] or 'N' in pair[0] or 'S' in pair[0]:
                if 'H' in pair[1]:
                    polars.append(pair)

            if 'O' in pair[1] or 'N' in pair[1] or 'S' in pair[1]:
                if 'H' in pair[0]:
                    polars.append(pair)

        # Find square root of all b_i values so that they can be added easily according to paper's formula.
        for atom in self.ddec_data:
            atom[-2] = (atom[-2]) ** 0.5

        if polars:
            for pair in polars:
                if 'H' in pair[0] or 'H' in pair[1]:
                    if 'H' in pair[0]:
                        polar_h_pos = int(pair[0][:-1]) - 1
                        polar_son_pos = int(pair[1][:-1]) - 1
                    else:
                        polar_h_pos = int(pair[1][:-1]) - 1
                        polar_son_pos = int(pair[0][:-1]) - 1

                    # Calculate the new b_i for the two polar atoms (polar h and polar sulfur, oxygen or nitrogen)
                    self.ddec_data[polar_son_pos][-2] += self.ddec_data[polar_h_pos][-2]
                    self.ddec_data[polar_h_pos][-2] = 0

        # Square all the b_i values again
        for atom in self.ddec_data:
            atom[-2] *= atom[-2]

        # Recalculate the a_i values
        for atom in self.ddec_data:
            atom[-1] = 32 * atom[-2] * (atom[-3] ** 6)

        # Update epsilon (not sigma) according to new a_i and b_i values
        for pos, atom in enumerate(self.ddec_data):

            if atom[-1] == 0:
                epsilon, self.non_bonded_force[pos][1] = 0, str(0)
            else:
                # epsilon = (b_i ** 2) / (4 * a_i)
                epsilon = (atom[-2] ** 2) / (4 * atom[-1])
                epsilon *= self.epsilon_conversion

            self.non_bonded_force[pos] = [str(atom[5]), self.non_bonded_force[pos][1], str(epsilon)]

    def apply_symmetrisation(self):
        """Using the atoms picked out to be symmetrised apply the symmetry to the charge, sigma and epsilon values"""

        # get the values to be symmetrised
        for sym_set in self.molecule.symm_hs.values():
            charges, sigmas, epsilons = [], [], []
            for atom_set in sym_set:
                for atom in atom_set:
                    charges.append(float(self.non_bonded_force[atom - 1][0]))
                    sigmas.append(float(self.non_bonded_force[atom - 1][1]))
                    epsilons.append(float(self.non_bonded_force[atom - 1][2]))
                # calculate the average values to be used in symmetry
                charge, sigma, epsilon = sum(charges) / len(charges), sum(sigmas) / len(sigmas), sum(epsilons) / len(epsilons)

                # now loop through the atoms again and store the new values
                for atom in atom_set:
                    self.non_bonded_force[atom - 1] = [str(charge), str(sigma), str(epsilon)]

    def extract_extra_sites(self):
        """
        1) Gather the extra sites from the XYZ find parent and 2 reference atoms
        2) calculate the local coords site
        3) save the charge
        4) return back to the molecule
        (users have the option to use sites or no sites this way)
        """

        # weighting arrays for the virtual sites should not be changed
        w1o, w2o, w3o = 1.0, 0.0, 0.0  # SUM SHOULD BE 1
        w1x, w2x, w3x = -1.0, 1.0, 0.0  # SUM SHOULD BE 0
        w1y, w2y, w3y = -1.0, 0.0, 1.0  # SUM SHOULD BE 0

        with open('xyz_with_extra_point_charges.xyz') as xyz_sites:
            lines = xyz_sites.readlines()

        sites = OrderedDict()
        sites_no = 0
        for i, line in enumerate(lines[2:]):
            # get the current element
            element = str(line.split()[0])

            if element != 'X':
                # search the following entries for sites connected to this atom
                for pos_site in lines[i + 3:]:
                    if str(pos_site.split()[0]) != 'X':
                        # if there are no sites break and start the next loop
                        break
                    else:
                        # get the virtual site coords
                        v_pos = array([float(pos_site.split()[x]) for x in range(1, 4)])
                        # get parent index number for the topology network
                        parent = i + 1 - sites_no
                        # get the two closest atoms to the parent
                        closet_atoms = list(self.molecule.topology.neighbors(parent))
                        if len(closet_atoms) < 2:
                            # find another atom if we only have one
                            # dont want to get the parent as a close atom
                            closet_atoms.append(list(self.molecule.topology.neighbors(closet_atoms[0]))[-1])

                        # Get the xyz coordinates of the reference atoms
                        parent_pos = array(self.molecule.molecule['qm'][parent - 1][1:])
                        close_a = array(self.molecule.molecule['qm'][closet_atoms[0] - 1][1:])
                        close_b = array(self.molecule.molecule['qm'][closet_atoms[1] - 1][1:])

                        # work out the local coordinates site using rules from the OpenMM guide
                        orig = w1o * parent_pos + w2o * close_a + close_b * w3o
                        ab = w1x * parent_pos + w2x * close_a + w3x * close_b  # rb-ra
                        ac = w1y * parent_pos + w2y * close_a + w3y * close_b  # rb-ra
                        # Get the axis unit vectors
                        z_dir = cross(ab, ac)
                        z_dir = z_dir / sqrt(dot(z_dir, z_dir.reshape(3, 1)))
                        x_dir = ab / sqrt(dot(ab, ab.reshape(3, 1)))
                        y_dir = cross(z_dir, x_dir)
                        # Get the local coordinates positions
                        p1 = dot((v_pos - orig), x_dir.reshape(3, 1))
                        p2 = dot((v_pos - orig), y_dir.reshape(3, 1))
                        p3 = dot((v_pos - orig), z_dir.reshape(3, 1))

                        charge = float(pos_site.split()[4])

                        # store the site info [(parent top no, a, b), (p1, p2, p3), charge]]
                        sites[sites_no] = [(parent - 1, closet_atoms[0] - 1, closet_atoms[1] - 1), (p1 / 10, p2 / 10, p3 / 10), charge]
                        sites_no += 1

        self.molecule.sites = sites

        # get the parent non bonded values
        for value in sites.values():
            charge, sigma, eps = self.non_bonded_force[value[0][0]]
            # Change the charge on the first entry
            charge = float(charge) - value[2]
            self.non_bonded_force[value[0][0]] = [str(charge), sigma, eps]

    def calculate_non_bonded_force(self):
        """
        Main worker method for LennardJones class. Extracts necessary parameters from ONETEP or Chargemol files;
        Calculates the a_i and b_i values;
        Calculates the sigma and epsilon values using those a_i and b_i values;
        Redistributes L-J parameters according to polar Hydrogens, then recalculates epsilon values.
        returns non_bonded_force for the XML creator in Ligand class.
        """

        if self.qm['charges_engine'] == 'chargemol':
            self.extract_params_chargemol()

        elif self.qm['charges_engine'] == 'onetep':
            self.extract_params_onetep()

        else:
            raise KeyError('Invalid charges engine provided, cannot extract charges.')

        # Calculate initial a_is and b_is
        self.append_ais_bis()

        # Use the a_is and b_is to calculate the non_bonded_force dict
        self.calculate_sig_eps()

        # Tweak for polar Hydrogens
        self.correct_polar_hydrogens()

        # Tweak the charge, sigma and epsilon for symmetry
        self.apply_symmetrisation()

        # Find extra site positions in local coords if present and tweak the charges of the parent
        if self.qm['charges_engine'] == 'onetep':
            self.extract_extra_sites()

        return self.non_bonded_force
