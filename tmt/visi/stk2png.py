import os
import tifffile
import png
import numpy as np
from copy import copy
import re
from tmt.illuminati import stitch


# TODO:
# - single site acquisitions with no well/row/column info
# - other acquisition modes, e.g. vertical


def read_stk(stk_filename):
    '''
    Read stk file from disk and unpack individual stacks.

    Parameters
    ----------
    stk_filename: str

    Returns
    -------
    numpy.ndarray
    '''
    return tifffile.imread(stk_filename)


def write_png(png_filename, image):
    '''
    Write image to disk as png.

    Parameters
    ----------
    png_filename: str
    image: numpy.ndarray
    '''
    with open(png_filename, 'wb') as f:
        writer = png.Writer(width=image.shape[1],
                            height=image.shape[0],
                            bitdepth=16, greyscale=True)
        im_list = image.tolist()
        writer.write(f, im_list)


def read_nd(nd_filename):
    '''
    Read the content of the .nd file and do some pre-formatting.

    Parameters
    ----------
    nd_filename: str

    Returns
    -------
    Dict[str, str or List[str]]
    '''
    with open(nd_filename, 'r') as f:
        content = f.readlines()

    nd = dict()
    for line in content:
        if re.search('NDInfoFile', line) or re.search('EndFile', line):
            continue
        matched = re.search(r'"(.*)", (.+)\r', line)
        key = matched.group(1)
        value = matched.group(2)
        nd[key] = value
    return nd


def format_nd(nd):
    '''
    Format .nd file content, i.e. translate it into python syntax.

    Important keys:

        - DoStage
        - DoTimelapse
        - DoWave
        - DoZSeries

    Parameters
    ----------
    nd: Dict[str, str or List[str]]
        unformatted content of an .nd file

    Returns
    -------
    Dict[str, str or List[str]]
        formatted content of an .nd file
    '''
    for k, v in nd.iteritems():
        string_match = re.search(r'"(.+)"', v)
        number_match = re.search(r'(\d+)', v)
        if v == 'TRUE':
            nd[k] = True
        elif v == 'FALSE':
            nd[k] = False
        elif string_match:
            nd[k] = string_match.group(1)
        elif number_match:
            nd[k] = int(number_match.group(1))
    return nd


class Stk2png(object):
    '''
    Class for converting .stk files to .png images
    (with optional file renaming).
    '''

    def __init__(self, input_files, nd_file, config):
        '''
        Initialize Stk2png class.

        Parameters
        ----------
        input_files: List[str]
            .stk filenames
        nd_file: str
            filename of the corresponding .nd file
        config: Dict[str, str]
            configuration settings (from YAML config file)
        '''
        self.input_files = map(os.path.basename, input_files)
        self.input_dir = os.path.dirname(input_files[0])
        self.nd_file = os.path.basename(nd_file)
        self.info = dict()
        self.filename_pattern = '(?:sdc|dualsdc|hxp|tirf|led)(.*)_s(\d+).stk'
        self.filter_pattern = '.*?([^mx]+[0-9]?)(?=xm)'
        self.tokens = ['filter', 'site']
        self.nomenclature = copy(config['FILENAME_FORMAT'])
        self.acquisition_mode = copy(config['ACQUISITION_MODE'])
        self.acquisition_layout = copy(config['ACQUISITION_LAYOUT'])

    def extract_info_from_nd_files(self):
        '''
        Extract meta information from .nd files,
        in particular information on the position within the well plate.
        '''
        nd_filename = os.path.join(self.input_dir, self.nd_file)
        nd = read_nd(nd_filename)
        nd = format_nd(nd)

        metainfo = dict()
        metainfo['hasWell'] = nd['DoStage']
        metainfo['hasTime'] = nd['DoTimelapse']
        metainfo['hasChannel'] = nd['DoWave']
        metainfo['hasZStack'] = nd['DoZSeries']
        metainfo['nrChannel'] = nd['NWavelengths']
        metainfo['hasCycle'] = False
        self.metainfo = metainfo

        def extract_well_ids(nd, files):
            '''
            Extract information on well position from .nd file content.

            Returns
            --------
            List[str]
            '''
            wells = [v for k, v in nd.iteritems() if re.search(r'Stage\d+', k)]
            well_info = [re.search(r'row:(\w),column:(\d+)', w) for w in wells]
            well_ids = [''.join(map(w.group, xrange(1, 3))) for w in well_info]
            return well_ids
        self.nr_sites = nd['NStagePositions']

        if self.metainfo['hasWell']:
            well_ids = extract_well_ids(nd, self.input_files)
            self.info['well'] = well_ids

    def extract_info_from_filenames(self):
        '''
        Extract image information (such as channel, site, etc.) from filenames.
        '''
        info = self.info
        pattern = self.filename_pattern
        for key in self.tokens:
            info[key] = list()
        # Handle exceptions for 'time' and 'channel', which are not always
        # present in the filename
        if self.metainfo['hasTime']:
            pattern = re.sub(r'(.*)\.stk$', '\\1_t(\d+).stk', pattern)
            self.tokens = self.tokens + ['time']
            info['time'] = []
        else:
            info['time'] = [1 for x in xrange(len(self.input_files))]
        if self.metainfo['hasChannel']:
            pattern = '%s%s' % ('_w(\d)', pattern)
            self.tokens = ['channel'] + self.tokens
            info['channel'] = []
        else:
            info['channel'] = [0 for x in xrange(len(self.input_files))]
        # Information on zstacks is handled separately
        info['zstack'] = [0 for x in xrange(len(self.input_files))]
        # The project name can be easily retrieved from .nd filename
        exp_name = re.search(r'(.*)\.nd$', self.nd_file).group(1)
        info['project'] = [exp_name for x in xrange(len(self.input_files))]

        for stk_file in self.input_files:
            # Retrieve information for 'filter' from filename
            r = re.compile('%s%s' % (exp_name, pattern))
            matched = re.search(r, stk_file)
            if not matched:
                raise ValueError('"filter" info could not be determined \
                                 from filenames')
            matched = map(matched.group, range(1, len(self.tokens)+1))
            for i, t in enumerate(self.tokens):
                # Handle special cases that are more complex
                if t == 'filter':
                    if re.search(r'xm', matched[i]):
                        r = re.compile(self.filter_pattern)
                        match = re.search(r, matched[i]).group(1)
                    # TODO: handles dual sdc mode!
                    else:
                        match = matched[i]
                else:
                    match = matched[i]
                try:
                    info[t].append(match)
                except:
                    raise ValueError('... Token "%s" didn\'t match filename.' % t)
        # Assert correct data type (string, interger, etc)
        convert_to_int = ['site', 'channel']
        for i in convert_to_int:
            if i in info:
                info[i] = map(int, info[i])
        self.info = info

        if 'well' in self.info and self.metainfo['hasWell']:
            # Map well ids to filenames:
            # We simply loaded the well information for all sites form nd file,
            # now we have to extract the relevant info for the processed sites
            # They are sorted according to "site".
            self.info['well'] = [self.info['well'][i-1]  # sites are one-based!
                                 for i in self.info['site']]
        else:
            self.info['well'] = [0 for x in xrange(len(self.input_files))]

    def calc_image_position(self):
        '''
        Calculate the y,x (row, column) position of images
        within the continuous acquisition grid, i.e. the well.
        '''
        if self.acquisition_mode == 'ZigZagHorizontal':
            doZigZag = True
        elif self.acquisition_mode == 'Horizontal':
            doZigZag = False
        else:
            raise ValueError('The provided acquisition mode is not supported.')

        # determine acquisition grid layout a.k.a. image "snake"
        stitch_dims = stitch.guess_stitch_dimensions(self.nr_sites,
                                                     self.acquisition_layout)
        snake = stitch.calc_image_position(stitch_dims, doZigZag)
        # find "row" and "column" position of processed sites within the grid
        sites = np.array(self.info['site'])
        column = np.array([None for x in xrange(len(sites))])
        row = np.array([None for x in xrange(len(sites))])
        for i in xrange(self.nr_sites):
            j = i+1  # sites are one-based
            if j not in sites:
                continue
            ix = np.where(sites == j)[0]
            row[ix] = snake[i][0]
            column[ix] = snake[i][1]
        self.info['row'] = map(int, row)
        self.info['column'] = map(int, column)

    def format_filenames(self, rename=True):
        '''
        If files should be renamed, first extract required information from
        metadata files (.nd) and filenames (.stk) and then format filenames
        according to nomenclature defined in configuration settings.
        Otherwise, simply replace ".stk" suffix with ".png".

        Parameters
        ----------
        rename: bool, optional
            whether files should be renamed according to configuration settings
            (defaults to True)
        '''
        if rename:
            # Get required information for renaming
            self.extract_info_from_nd_files()
            self.extract_info_from_filenames()
            self.calc_image_position()
            # Format filename strings according to configuration settings
            self.output_files = list()
            for i in xrange(len(self.input_files)):
                f = self.nomenclature
                output_filename = f.format(project=self.info['project'][i],
                                           well=self.info['well'][i],
                                           site=self.info['site'][i],
                                           row=self.info['row'][i],
                                           column=self.info['column'][i],
                                           zstack=self.info['zstack'][i],
                                           time=self.info['time'][i],
                                           filter=self.info['filter'][i],
                                           channel=self.info['channel'][i])
                self.output_files.append(output_filename)
        else:
            # Replace .stk suffix with .png
            self.output_files = [re.sub(r'stk$', 'png', f)
                                 for f in self.input_files]

    def unpack_images(self, output_dir, output_files=None, keep_z=False):
        '''
        Read and upstack stk files and write images (z-stacks or MIPs)
        to disk as .png files.

        Parameters
        ----------
        output_dir: str
            directory where images should be saved
        output_files: List[str], optional
            list of output filenames
        keep_z: bool, optional
            keep individual z stacks (True) or perform maximum intensity
            projection (False - default)
        '''
        if output_files is None:
            if not self.output_files:
                raise IOError('Output files were not provided are could not '
                              'be retrieved either.')
            output_files = self.output_files
        for i in xrange(len(self.input_files)):
            stk_file = self.input_files[i]
            print '.... Unpack file "%s"' % os.path.join(self.input_dir,
                                                         stk_file)
            stack = read_stk(os.path.join(self.input_dir, stk_file))
            output_file = output_files[i]
            output_filename = os.path.join(output_dir, output_file)
            if keep_z and self.metainfo['hasWell']:
                # Keep individual z-stacks
                for z in xrange(stack.shape[0]):
                    # Encode 'zstack' info in filename
                    output_file = re.sub(r'_z00', '_z%.2d' % z, output_file)
                    print '.... Write file "%s"' % output_filename
                    write_png(output_filename, stack[z])
            else:
                # Perform maximum intensity projection (MIP)
                # Should also work if there is only one image (i.e. no stacks)
                mip = np.array(np.max(stack, axis=0), dtype=stack[0].dtype)
                print '.... Write file "%s"' % output_filename
                write_png(output_filename, mip)
