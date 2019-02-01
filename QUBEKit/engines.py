#!/usr/bin/env python

# TODO Expand the functional_dict for PSI4 and Gaussian classes to "most" functionals.
# TODO Eliminate sub_call where possible.
# TODO Add better error handling for missing info. (Done for file extraction.)
#       Maybe add path checking for chargemol?


from QUBEKit.helpers import get_overage, check_symmetry, append_to_log
from QUBEKit.decorators import for_all_methods, timer_logger

from subprocess import call as sub_call
from numpy import array, zeros


class Engines:
    """Engines superclass containing core information that all other engines (PSI4, Gaussian etc) will have.
    Provides atoms' coordinates with name tags for each atom and entire molecule.
    Also gives all configs from the appropriate config file.
    """

    def __init__(self, molecule, config_dict):

        self.engine_mol = molecule
        self.charge = config_dict[0]['charge']
        self.multiplicity = config_dict[0]['multiplicity']
        # Load the configs using the config_file name from the csv.
        self.qm, self.fitting, self.descriptions = config_dict[1], config_dict[2], config_dict[3]

    def __repr__(self):
        return f'{self.__class__.__name__}({self.__dict__!r})'


@for_all_methods(timer_logger)
class PSI4(Engines):
    """Writes and executes input files for psi4.
    Also used to extract Hessian matrices; optimised structures; frequencies; etc.
    """

    def __init__(self, molecule, config_dict):

        super().__init__(molecule, config_dict)

        self.functional_dict = {'PBEPBE': 'PBE'}
        if self.qm['theory'] in list(self.functional_dict.keys()):
            self.qm['theory'] = self.functional_dict[self.qm['theory']]

    def generate_input(self, QM=False, MM=False, optimize=False, hessian=False, density=False, threads=False,
                       fchk=False, run=True):
        """Converts to psi4 input format to be run in psi4 without using geometric"""

        if QM:
            molecule = self.engine_mol.QMoptimized
        elif MM:
            molecule = self.engine_mol.MMoptimized
        else:
            molecule = self.engine_mol.molecule

        # input.dat is the psi4 input file.
        setters = ''
        tasks = ''

        # opening tag is always writen
        with open('input.dat', 'w+') as input_file:
            input_file.write('memory {} GB\n\nmolecule {} {{\n{} {} \n'.format(self.qm['threads'], self.engine_mol.name,
                                                                               self.charge, self.multiplicity))
            # molecule is always printed
            for atom in molecule:
                input_file.write(' {}    {: .10f}  {: .10f}  {: .10f} \n'.format(atom[0], float(atom[1]), float(atom[2]), float(atom[3])))
            input_file.write(' units angstrom\n no_reorient\n}}\n\nset {{\n basis {}\n'.format(self.qm['basis']))

            if energy:
                print('Writing psi4 energy calculation input')
                tasks += f"\nenergy  = energy('{self.qm['theory']}')"

            if optimize:
                append_to_log(self.engine_mol.log_file, 'Writing PSI4 optimisation input', 'minor')
                setters += ' g_convergence {}\n GEOM_MAXITER {}\n'.format(self.qm['convergence'], self.qm['iterations'])
                tasks += "\noptimize('{}')".format(self.qm['theory'].lower())

            if hessian:
                append_to_log(self.engine_mol.log_file, 'Writing PSI4 Hessian matrix calculation input', 'minor')
                setters += ' hessian_write on\n'

                tasks += "\nenergy, wfn = frequency('{}', return_wfn=True)".format(self.qm['theory'].lower())

                tasks += '\nwfn.hessian().print_out()\n\n'

            if density:
                append_to_log(self.engine_mol.log_file, 'Writing PSI4 density calculation input', 'minor')
                setters += " cubeprop_tasks ['density']\n"

                overage = get_overage(self.engine_mol.name)
                setters += " CUBIC_GRID_OVERAGE [{0}, {0}, {0}]\n".format(overage)
                setters += " CUBIC_GRID_SPACING [0.13, 0.13, 0.13]\n"
                tasks += "grad, wfn = gradient('{}', return_wfn=True)\ncubeprop(wfn)".format(self.qm['theory'].lower())

            if fchk:
                append_to_log(self.engine_mol.log_file, 'Writing PSI4 input file to generate fchk file')
                tasks += '\ngrad, wfn = gradient("{}", return_wfn=True)'.format(self.qm['theory'].lower())
                tasks += '\nfchk_writer = psi4.core.FCHKWriter(wfn)'
                tasks += '\nfchk_writer.write("{}_psi4.fchk")\n'.format(self.engine_mol.name)

            # TODO If overage cannot be made to work, delete and just use Gaussian.
            # if self.qm['solvent']:
            #     setters += ' pcm true\n pcm_scf_type total\n'
            #     tasks += '\n\npcm = {'
            #     tasks += '\n units = Angstrom\n Medium {\n  SolverType = IEFPCM\n  Solvent = Chloroform\n }'
            #     tasks += '\n Cavity {\n  RadiiSet = UFF\n  Type = GePol\n  Scaling = False\n  Area = 0.3\n  Mode = Implicit'
            #     tasks += '\n }\n}'

            setters += '}\n'

            # TODO Always use threads? sub_call below currently ignores True/False argument input anyway.
            if threads:
                setters += f'set_num_threads({self.qm["threads"]})\n'

            input_file.write(setters)
            input_file.write(tasks)

        if run:
            sub_call(f'psi4 input.dat -n {self.qm["threads"]}', shell=True)

    def hessian(self):
        """Parses the Hessian from the output.dat file (from psi4) into a numpy array.
        Molecule is a numpy array of size N x N.
        """

        hess_size = 3 * len(self.engine_mol.molecule)

        # output.dat is the psi4 output file.
        with open('output.dat', 'r') as file:

            lines = file.readlines()

            for count, line in enumerate(lines):
                if '## Hessian' in line:
                    # Set the start of the hessian to the row of the first value.
                    hess_start = count + 5
                    break
            else:
                raise EOFError('Cannot locate Hessian matrix in output.dat file.')

            # Check if the hessian continues over onto more lines (i.e. if hess_size is not divisible by 5)
            extra = 0 if hess_size % 5 == 0 else 1

            # hess_length: # of cols * length of each col
            #            + # of cols - 1 * #blank lines per row of hess_vals
            #            + # blank lines per row of hess_vals if the hess_size continues over onto more lines.
            hess_length = (hess_size // 5) * hess_size + (hess_size // 5 - 1) * 3 + extra * (3 + hess_size)

            hess_end = hess_start + hess_length

            hess_vals = []

            for file_line in lines[hess_start:hess_end]:
                # Compile lists of the 5 Hessian floats for each row.
                # Number of floats in last row may be less than 5.
                # Only the actual floats are added, not the separating numbers.
                row_vals = [float(val) for val in file_line.split() if len(val) > 5]
                hess_vals.append(row_vals)

            # Remove blank list entries
            hess_vals = [elem for elem in hess_vals if elem]

            reshaped = []

            # Convert from list of (lists, length 5) to 2d array of size hess_size x hess_size
            for old_row in range(hess_size):
                new_row = []
                for col_block in range(hess_size // 5 + extra):
                    new_row += hess_vals[old_row + col_block * hess_size]

                reshaped.append(new_row)

            hess_matrix = array(reshaped)

            # Units conversion.
            conversion = 627.509391 / (0.529 ** 2)
            hess_matrix *= conversion

            check_symmetry(hess_matrix)

            return hess_matrix

    def optimised_structure(self):
        """Parses the final optimised structure from the output.dat file (from psi4) to a numpy array."""

        # Run through the file and find all lines containing '==> Geometry', add these lines to a list.
        # Reverse the list
        # from the start of this list, jump down to the first atom and set this as the start point
        # Split the row into 4 columns: centre, x, y, z.
        # Add each row to a matrix.
        # Return the matrix.

        # output.dat is the psi4 output file.
        with open('output.dat', 'r') as file:
            lines = file.readlines()
            # Will contain index of all the lines containing '==> Geometry'.
            geo_pos_list = []
            for count, line in enumerate(lines):
                if "==> Geometry" in line:
                    geo_pos_list.append(count)
                    break
            else:
                raise EOFError('Cannot locate optimised structure in output.dat file.')

            # Set the start as the last instance of '==> Geometry'.
            start_of_vals = geo_pos_list[-1] + 9

            opt_struct = []

            for row in range(len(self.engine_mol.molecule)):

                # Append the first 4 columns of each row, converting to float as necessary.
                struct_row = [lines[start_of_vals + row].split()[0]]
                for indx in range(3):
                    struct_row.append(float(lines[start_of_vals + row].split()[indx + 1]))

                opt_struct.append(struct_row)

        return opt_struct

    def all_modes(self):
        """Extract all modes from the psi4 output file."""

        # Find "post-proj  all modes"
        # Jump to first value, ignoring text.
        # Move through data, adding it to a list
        # continue onto next line.
        # Repeat until the following line is known to be empty.

        # output.dat is the psi4 output file.
        with open('output.dat', 'r') as file:
            lines = file.readlines()
            for count, line in enumerate(lines):
                if "post-proj  all modes" in line:
                    start_of_vals = count
                    break
            else:
                raise EOFError('Cannot locate modes in output.dat file.')

            # Barring the first (and sometimes last) line, dat file has 6 values per row.
            end_of_vals = start_of_vals + (3 * len(self.engine_mol.molecule)) // 6

            structures = lines[start_of_vals][24:].replace("'", "").split()
            structures = structures[6:]

            for row in range(1, end_of_vals - start_of_vals):
                # Remove double strings and weird formatting.
                structures += lines[start_of_vals + row].replace("'", "").replace("]", "").split()

            all_modes = [float(val) for val in structures]

            return array(all_modes)

    def geo_gradient(self, QM=False, MM=False, threads=False, run=True):
        """Write the psi4 style input file to get the gradient for geometric
        and run geometric optimisation.
        """

        if QM:
            molecule = self.engine_mol.QMoptimized

        elif MM:
            molecule = self.engine_mol.MMoptimized

        else:
            molecule = self.engine_mol.molecule

        with open(f'{self.engine_mol.name}.psi4in', 'w+') as file:

            file.write('molecule {} {{\n {} {} \n'.format(self.engine_mol.name, self.charge, self.multiplicity))
            for atom in molecule:
                file.write('  {}    {: .10f}  {: .10f}  {: .10f}\n'.format(atom[0], float(atom[1]), float(atom[2]), float(atom[3])))

            file.write("units angstrom\n no_reorient\n}}\nset basis {}\n".format(self.qm['basis']))

            if threads:
                file.write('set_num_threads({})'.format(self.qm['threads']))
            file.write("\n\ngradient('{}')\n".format(self.qm['theory']))

        if run:
            with open('log.txt', 'w+') as log:
                sub_call(f'geometric-optimize --psi4 {self.engine_mol.name}.psi4in --nt {self.qm["threads"]}',
                         shell=True, stdout=log)


@for_all_methods(timer_logger)
class Chargemol(Engines):

    def __init__(self, molecule, config_file):

        super().__init__(molecule, config_file)

    def generate_input(self, run=True):
        """Given a DDEC version (from the defaults), this function writes the job file for chargemol and
        executes it.
        """

        if (self.qm['ddec_version'] != 6) and (self.qm['ddec_version'] != 3):
            append_to_log(log_file=self.engine_mol.log_file,
                          message='Invalid or unsupported DDEC version given, running with default version 6.',
                          msg_type='warning')
            self.qm['ddec_version'] = 6

        # Write the charges job file.
        with open('job_control.txt', 'w+') as charge_file:

            charge_file.write(f'<input filename>\n{self.engine_mol.name}.wfx\n</input filename>')

            charge_file.write('\n\n<net charge>\n0.0\n</net charge>')

            charge_file.write('\n\n<periodicity along A, B and C vectors>\n.false.\n.false.\n.false.')
            charge_file.write('\n</periodicity along A, B and C vectors>')

            charge_file.write(f'\n\n<atomic densities directory complete path>\n{self.descriptions["chargemol"]}/atomic_densities/')
            charge_file.write('\n</atomic densities directory complete path>')

            charge_file.write(f'\n\n<charge type>\nDDEC{self.qm["ddec_version"]}\n</charge type>')

            charge_file.write('\n\n<compute BOs>\n.true.\n</compute BOs>')

        # sub_call(f'psi4 input.dat -n {self.qm["threads"]}', shell=True)
        # sub_call('mv Dt.cube total_density.cube', shell=True)

        if run:
            sub_call(f'{self.descriptions["chargemol"]}/chargemol_FORTRAN_09_26_2017/compiled_binaries/linux/Chargemol_09_26_2017_linux_serial job_control.txt',
                     shell=True)


@for_all_methods(timer_logger)
class Gaussian(Engines):
    """Writes and executes input files for Gaussian09.
    Also used to extract Hessian matrices; optimised structures; frequencies; etc.
    """

    def __init__(self, molecule, config_dict):

        super().__init__(molecule, config_dict)

        self.functional_dict = {'PBE': 'PBEPBE'}
        if self.qm['theory'] in list(self.functional_dict.keys()):
            self.qm['theory'] = self.functional_dict[self.qm['theory']]

    def generate_input(self, QM=False, MM=False, optimize=False, hessian=False, density=False, solvent=False, run=True):
        """Generates the relevant job file for Gaussian, then executes this job file."""

        if QM:
            molecule = self.engine_mol.QMoptimized
        elif MM:
            molecule = self.engine_mol.MMoptimized
        else:
            molecule = self.engine_mol.molecule

        with open(f'gj_{self.engine_mol.name}', 'w+') as input_file:

            input_file.write(f'%Mem={self.qm["memory"]}GB\n%NProcShared={self.qm["threads"]}\n%Chk=lig\n')

            commands = f'# {self.qm["theory"]}/{self.qm["basis"]} SCF=XQC '

            # Adds the commands in groups. They MUST be in the right order because Gaussian.
            if optimize:
                commands += 'opt '

            if hessian:
                commands += 'freq '

            if self.qm['solvent']:
                commands += 'SCRF=(IPCM,Read) '

            if density:
                commands += 'density=current OUTPUT=WFX '

            commands += f'\n\n{self.engine_mol.name}\n\n{self.charge} {self.multiplicity}\n'

            input_file.write(commands)

            # Add the atomic coordinates
            for atom in molecule:
                input_file.write('{} {: .3f} {: .3f} {: .3f}\n'.format(atom[0], float(atom[1]), float(atom[2]), float(atom[3])))

            if solvent:
                # Adds the epsilon and cavity params
                input_file.write('\n4.0 0.0004')

            if density:
                # Specify the creation of the wavefunction file
                input_file.write(f'\n{self.engine_mol.name}.wfx')

            # Blank lines because Gaussian.
            input_file.write('\n\n')

        if run:
            sub_call(f'g09 < gj_{self.engine_mol.name} > gj_{self.engine_mol.name}.log', shell=True)

    def hessian(self):
        """Extract the Hessian matrix from the Gaussian fchk file."""

        with open('lig.fchk', 'r') as fchk:

            lines = fchk.readlines()
            hessian_list = []

            for count, line in enumerate(lines):
                if line.startswith('Cartesian Force Constants'):
                    start_pos = count + 1
                if line.startswith('Dipole Moment'):
                    end_pos = count

            if not start_pos and end_pos:
                raise EOFError('Cannot locate Hessian matrix in lig.fchk file.')

            for line in lines[start_pos: end_pos]:
                # Extend the list with the converted floats from the file, splitting on spaces and removing '\n' tags.
                hessian_list.extend([float(num) * 0.529 for num in line.strip('\n').split()])

        hess_size = 3 * len(self.engine_mol.molecule)

        hessian = zeros((hess_size, hess_size))

        # Rewrite Hessian to full, symmetric 3N * 3N matrix rather than list with just the non-repeated values.
        m = 0
        for i in range(hess_size):
            for j in range(i + 1):
                hessian[i, j] = hessian_list[m]
                hessian[j, i] = hessian_list[m]
                m += 1

        check_symmetry(hessian)

        return hessian

    def optimised_structure(self):
        """Extract the optimised structure from the Gaussian log file."""

        with open(f'gj_{self.engine_mol.name}.log', 'r') as log_file:

            lines = log_file.readlines()

            opt_coords_pos = []
            for count, line in enumerate(lines):
                if 'Input orientation' in line:
                    opt_coords_pos.append(count + 5)
                    break
            else:
                raise EOFError(f'Cannot locate optimised structures in gj_{self.engine_mol.name}.log file.')

            start_pos = opt_coords_pos[-1]

            num_atoms = len(self.engine_mol.molecule)

            opt_struct = []

            for count, line in enumerate(lines[start_pos: start_pos + num_atoms]):

                vals = line.split()[-3:]
                vals = [self.engine_mol.molecule[count][0]] + [float(i) for i in vals]
                opt_struct.append(vals)

        return opt_struct

    def all_modes(self):
        """Extract the frequencies from the Gaussian log file."""

        with open(f'gj_{self.engine_mol.name}.log', 'r') as log_file:

            lines = log_file.readlines()
            freqs = []

            # Stores indices of rows which will be used
            freq_positions = []
            for count, line in enumerate(lines):
                if line.startswith(' Frequencies'):
                    freq_positions.append(count)
                    break
            else:
                raise EOFError(f'Cannot locate modes in gj_{self.engine_mol.name}.log file.')

            for pos in freq_positions:
                freqs.extend(float(num) for num in lines[pos].split()[2:])

        return array(freqs)
