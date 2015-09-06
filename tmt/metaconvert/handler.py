import os
import re
from cached_property import cached_property
from abc import ABCMeta
from abc import abstractproperty
from ..formats import Formats
from ..metadata_readers import BioformatsMetadataReader
from ..metadata import ChannelImageMetadata
from ..illuminati import stitch
from .. import utils
from ..errors import MetadataError
from ..errors import NotSupportedError
from ..plates import WellPlate


class MetadataHandler(object):

    '''
    Abstract base class for handling image data and associated metadata from
    heterogeneous microscope file formats as provided by the
    `Bio-Formats <http://www.openmicroscopy.org/site/products/bio-formats>`_
    library.

    Original metadata has to be available as OME-XML according to the
    `OME schema <http://www.openmicroscopy.org/Schemas/Documentation/Generated/OME-2015-01/ome.html>`_.

    The metadata that can be automatically retrieved form image files may not
    be sufficient, but may require additional microscope-specific metadata
    and/or user input.
    '''

    __metaclass__ = ABCMeta

    def __init__(self, image_upload_dir, additional_upload_dir, ome_xml_dir,
                 cycle_name):
        '''
        Initialize an instance of class MetadataHandler.

        Parameters
        ----------
        image_upload_dir: str
            directory where image files were uploaded to
        additional_upload_dir: str
            directory where additional microscope-specific metadata files
            may have been uploaded to
        ome_xml_dir: str
            directory where OMEXML metadata files were stored upon extraction
            of metadata from the image files in `image_upload_dir`
        cycle_name: str
            name of the cycle, i.e. the name of the folder of the corresponding
            experiment or subexperiment
        '''
        self.image_upload_dir = image_upload_dir
        self.additional_upload_dir = additional_upload_dir
        self.ome_xml_dir = ome_xml_dir
        self.cycle_name = cycle_name

    @property
    def supported_extensions(self):
        '''
        Returns
        -------
        Set[str]
            file extensions of supported formats
        '''
        self._supported_extensions = Formats().supported_extensions
        return self._supported_extensions

    @cached_property
    def image_files(self):
        '''
        Returns
        -------
        List[str]
            names of image files

        Note
        ----
        To be recognized as an image file, a file must have one of the
        supported file extensions.

        Raises
        ------
        OSError
            when no image files are found
        '''
        files = [f for f in os.listdir(self.image_upload_dir)
                 if os.path.splitext(f)[1] in self.supported_extensions]
        if len(files) == 0:
            raise OSError('No image files founds in folder: %s'
                          % self.image_upload_dir)
        self._image_files = files
        return self._image_files

    @cached_property
    def ome_xml_files(self):
        '''
        Returns
        -------
        List[str]
            names of the XML files that contain the extracted OMEXML data

        Raises
        ------
        OSError
            when no ome-xml files are found

        Note
        ----
        Files are supposed to have the same basename as the image file from
        which the metadata was extracted, but with *.ome.xml* extension.
        '''
        files = [f for f in os.listdir(self.ome_xml_dir)
                 if f.endswith('.ome.xml')]
        if len(files) == 0:
            raise OSError('No OMEXML files founds in folder: %s'
                          % self.ome_xml_dir)
        self._ome_xml_files = files
        return self._ome_xml_files

    @property
    def ome_image_metadata(self):
        '''
        Read the OMEXML metadata extracted from image files.

        Returns
        -------
        Dict[str, bioformats.omexml.OMEXML]
            metadata retrieved from image files

        See also
        --------
        `tmt.metadata_readers.BioformatsMetadataReader`_
        '''
        self._ome_image_metadata = dict()
        with BioformatsMetadataReader() as reader:
            for i, f in enumerate(self.ome_xml_files):
                filename = os.path.join(self.ome_xml_dir, f)
                k = self.image_files[i]
                self._ome_image_metadata[k] = reader.read(filename)
        return self._ome_image_metadata

    @staticmethod
    def _create_channel_planes(pixels):
        # Add new *Plane* elements to an existing OMEXML *Pixels* object,
        # such that z-stacks are grouped by channel.
        n_channels = pixels.SizeC
        n_zstacks = pixels.SizeZ
        pixels.plane_count = n_channels * n_zstacks
        channel_position = pixels.DimensionOrder.index('C')
        zstack_position = pixels.DimensionOrder.index('Z')
        if zstack_position < channel_position:
            count = -1
            for z in xrange(n_zstacks):
                for c in xrange(n_channels):
                    count += 1
                    pixels.Plane(count).TheZ = z
                    pixels.Plane(count).TheC = c
                    pixels.Plane(0).TheT = 0
        else:
            count = -1
            for c in xrange(n_channels):
                for z in xrange(n_zstacks):
                    count += 1
                    pixels.Plane(count).TheZ = z
                    pixels.Plane(count).TheC = c
                    pixels.Plane(0).TheT = 0
        return pixels

    def _create_custom_image_metadata(self, ome_image_element):
        # Create an instance of class ChannelImageMetadata for each channel
        # specified in an OMEXML *Image* element.
        # It is assumed that all *Plane* elements where
        # acquired at the same site, i.e. microscope stage position.
        image_metadata = list()
        pixels = ome_image_element.Pixels

        n_timepoints = pixels.SizeT
        if n_timepoints > 1:
            raise NotSupportedError('Only images with a single timepoint '
                                    'are supported.')

        n_planes = pixels.plane_count
        if n_planes == 0:
            # Sometimes an image doesn't have any planes, but still
            # contains multiple channels and/or z-stacks.
            # Let's create new plane elements for consistency.
            pixels = self._create_channel_planes(pixels)
            n_planes = pixels.plane_count  # update plane count

        n_channels = pixels.SizeC
        for c in xrange(n_channels):
            md = ChannelImageMetadata()
            md.cycle = self.cycle_name
            md.name = ome_image_element.Name
            md.original_dtype = pixels.PixelType
            md.original_dimensions = (pixels.SizeY, pixels.SizeX)
            md.channel = pixels.Channel(c).Name
            planes = {p: pixels.Plane(p) for p in xrange(n_planes)
                      if c == pixels.Plane(p).TheC}
            md.original_planes = planes.keys()
            md.position = (planes.values()[0].PositionY,
                           planes.values()[0].PositionX)
            image_metadata.append(md)

        return image_metadata

    def _update_custom_image_metadata(self, ome_image_element, metadata):
        # Update attribute values of an existing instances of class
        # ChannelImageMetadata.
        updated_metadata = list(metadata)
        pixels = ome_image_element.Pixels

        n_planes = pixels.plane_count
        n_channels = pixels.SizeC
        if n_channels is not len(metadata):
            raise AssertionError('Number of channels must be identical.')
        for c in xrange(n_channels):
            md = updated_metadata[c]
            # There must be a naicer way...
            if not md.name:
                try:
                    md.name = ome_image_element.Name
                except:
                    pass
            if not md.original_dtype:
                try:
                    md.original_dtype = pixels.PixelType
                except:
                    pass
            if not any(md.original_dimensions):
                try:
                    md.original_dimensions = (pixels.SizeY, pixels.SizeX)
                except:
                    pass
            if not md.channel:
                try:
                    md.channel = pixels.Channel(c).Name
                except:
                    pass
            if not any(md.position):
                try:
                    planes = [pixels.Plane(p) for p in xrange(n_planes)
                              if c == pixels.Plane(p).TheC]
                    md.position = (planes[0].PositionY, planes[0].PositionX)
                except:
                    pass
            updated_metadata[c] = md

        return updated_metadata

    def format_image_metadata(self):
        '''
        Convert image metadata from `OMEXML` into custom format.

        Returns
        -------
        List[ChannelImageMetadata]
            formatted metadata objects

        Note
        ----
        There must be one *OMEXML* object for each image file.
        An image file, however, may contain more than one *Image* element,
        which is referred to as a *series*.
        Each *Image*/*Pixels* element contains at least one *Plane* element.
        A *Plane* represents a 2-dimensional pixel array for each channel,
        z-section or timepoint. The different planes are often grouped
        together as a *series* per acquisition site, i.e. microscope stage
        position (but this doesn't have to be the case).
        Ultimately, we would like to create image files that contain only
        a single-channel plane image per file. To this end, we group planes per
        channel. In the simplest case, there is only one plane per
        channel for a given *Image* element. If images were acquired at
        multiple z resolutions, they will be subsequently projected to 2D.
        Multiple timepoints are not supported.

        Raises
        ------
        NotSupportedError
            when metadata specifies more than one timepoint for an *Image*
            element

        See also
        --------
        `metadata.ChannelImageMetadata`_
        '''
        formatted_metadata = list()
        for i, f in enumerate(self.ome_image_metadata.keys()):
            n_series = self.ome_image_metadata[f].image_count
            # The number of series corresponds to the number of planes
            # within the image file.
            for j in xrange(n_series):
                image = self.ome_image_metadata[f].image(j)
                md = self._create_custom_image_metadata(image)
                for m in md:
                    m.original_filename = f
                    m.original_series = j
                formatted_metadata.extend(md)

        return formatted_metadata

    @abstractproperty
    def additional_files(self):
        '''
        Returns
        -------
        List[str] or None
            names of additional microscope-specific metadata files
        '''
        pass

    @abstractproperty
    def ome_additional_metadata(self):
        '''
        Returns
        -------
        bioformats.omexml.OMEXML
            metadata retrieved from additional microscope specific files
        '''
        pass

    def add_additional_metadata(self, metadata):
        '''
        Convert *OMEXML* metadata retrieved form additional microscope-specific
        metadata files into custom format and add it the metadata retrieved
        from image files.

        Additional metadata files contain information that is not available
        from individual image files, for example information about wells in
        case of a well plate format.

        Parameters
        ----------
        metadata: List[ChannelImageMetadata]
            image metadata

        Returns
        -------
        List[ChannelImageMetadata]
            complemented image metadata

        Note
        ----
        Since image-specific information is stored in *Image* elements and
        plate-specific information in a separate *Plate* element, one needs
        references from wells to individual images. This can be achieved by
        via *ImageRef* elements, which have to be set for each *WellSample*
        element in a plate. These references must be provided as substrings of
        the image filenames together with a matching regular expression string.

        Warning
        -------
        There must be only one *OMEXML* object for all image files.
        This is in contrast to the metadata for individual images, where there
        is a separate *OMEXML* object for each image file. The
        microscope-specific readers are responsible to ensure that the *image
        count* matches.
        *image count* = *number of channels* x *number of sites*

        Warning
        -------
        *Image* elements with *Name* "default.png" are automatically created by
        `python-bioformats` when *image_count* is set. They are assumed to be
        empty and are ignored.

        Raises
        ------
        NotSupportedError
            when metadata specifies more than one *Plate* element or more
            than one timepoint for an *Image* element or when *Plane* elements
            have different x, y positions
        MetadataError
            when *Plate* element provide no or incorrect references to
            image files or when no additional metadata is available

        See also
        --------
        `tmt.metadata.ChannelImageMetadata`_
        '''
        if self.ome_additional_metadata is None:
            raise MetadataError('No additional metadata available')
        complemented_metadata = list(metadata)
        n_images = self.ome_additional_metadata.image_count
        # The number of images corresponds to the total number of
        # single-channel planes, i.e. the number of final image files that will
        # get extracted from the original image files and saved as PNG files.
        for i in xrange(n_images):
            if self.ome_additional_metadata.image(i).Name == 'default.png':
                # Setting the image count automatically creates empty image
                # elements with name "default.png". They can be skipped.
                continue
            image = self.ome_additional_metadata.image(i)
            # TODO: this might be dangerous because it may happen that the
            # name of the image could not be determined or is not provided
            # by the microscope.
            matched_objects = {ix: md for ix, md in enumerate(metadata)
                               if md.name == image.Name}
            updated_objects = self._update_custom_image_metadata(
                                    image, matched_objects.values())
            for j, ix in enumerate(matched_objects.keys()):
                complemented_metadata[ix] = updated_objects[j]

        # Is there a *Plate* element specified?
        plates = self.ome_additional_metadata.plates
        n_plates = len(plates)
        if n_plates == 0:
            for i in xrange(n_images):
                complemented_metadata[i].well = ''
                return complemented_metadata
        elif n_plates > 1:
            raise NotSupportedError('Only a single plate is supported.')
        n_wells = len(plates[0].Well)
        well_inf = dict()
        # User regular expression to find reference *Image* elements
        ref_regexp = re.compile(plates[0].Well[0].Sample[0].ImageRef.keys()[0])
        if not ref_regexp:
            raise MetadataError('No reference to image files.')
        for w in xrange(n_wells):
            well_row = plates[0].Well[w].Row
            well_col = plates[0].Well[w].Column
            well_pos = (well_row, well_col)
            well_samples = plates[0].Well[w].Sample
            ref_values = [well_samples[i].ImageRef.values()[0]
                          for i in xrange(len(well_samples))]
            if any(ref_values):
                ref_values = utils.flatten(ref_values)
            well_inf.update({n: well_pos for n in ref_values})

        if all(well_inf.keys()):
            for md in complemented_metadata:
                ref_img = re.search(ref_regexp, md.original_filename)
                if ref_img:
                    ref_img = ref_img.group(1)
                else:
                    raise MetadataError('Incorrect reference to image files.')
                ref_well = [well for ref_well, well in well_inf.iteritems()
                            if ref_well == ref_img]
                if len(ref_well) > 1:
                    raise MetadataError('Incorrect reference to image files.')
                else:
                    md.well = WellPlate.well_position_to_id(ref_well[0])
        else:
            raise MetadataError('Incorrect reference to image files.')

        return complemented_metadata

    def determine_missing_ome_image_metadata(self, metadata):
        '''
        Determine, which of the required metadata information is not available.

        Parameters
        ----------
        metadata: List[ChannelMetadata]
            image metadata

        Returns
        -------
        Dict[int, str]
            index of the element and the name of attribute for which
            information is missing

        See also
        --------
        `tmt.metadata.ChannelImageMetadata`_
        '''
        missing = {i: k for i, md in enumerate(metadata)
                   for k, v in md.iteritems()
                   if k in ChannelImageMetadata.required and v is None}
        self.missing_metadata = missing
        return self.missing_metadata

    def add_user_metadata(self, metadata):
        '''
        Complement metadata with information provided by the user.

        Parameters
        ----------
        metadata: List[ChannelImageMetadata]
            image metadata

        Returns
        -------
        List[ChannelImageMetadata]
            complemented image metadata

        Returns
        -------
        List[ChannelImageMetadata]
            complemented metadata
        '''
        print 'TODO'
        # TODO: user input saved as json, which we can read here

    @staticmethod
    def _calculate_coordinates(positions):
        coordinates = stitch.calc_image_coordinates(positions)
        return coordinates

    def determine_grid_coordinates(self, metadata):
        '''
        Determine the position of each image acquisition site relative to its
        corresponding acquisition grid (slide or well in a plate).
        To this end, calculate the relative positions (coordinates) of images
        within each acquisition grid based on the absolute stage positions.

        Parameters
        ----------
        metadata: List[ChannelImageMetadata]
            metadata for each channel plane

        Returns
        -------
        List[ChannelImageMetadata]
            complemented metadata

        Raises
        ------
        MetadataError
            when metadata has no "position" attribute

        See also
        --------
        `illuminati.stitch.calc_image_coordinates`_
        '''
        # Retrieve the stage positions for each pixel array.
        if not any([hasattr(md, 'position') for md in metadata]):
            raise MetadataError('Metadata requires "position" attribute '
                                'for determination of grid coordinates.')
        if not any(utils.flatten([md.position for md in metadata])):
            raise MetadataError('Positions are not specified. Grid '
                                'coordinates can therefore not be determined.')
        complemented_metadata = list(metadata)
        all_positions = list()
        if metadata[0].well:
            wells = list(set([md.well for md in metadata]))
            for w in wells:
                positions = {i: md.position for i, md in enumerate(metadata)
                             if md.well == w}
                all_positions.append(positions)

        else:
            positions = {i: md.position for i, md in enumerate(metadata)}
            all_positions.append(positions)

        for p in all_positions:
            index, positions = p.keys(), p.values()
            coordinates = self._calculate_coordinates(positions)
            for i in xrange(len(index)):
                # All positional indices are one-based!
                complemented_metadata[index[i]].site = i+1  # TODO: sorting?
                complemented_metadata[index[i]].row = coordinates[i][0]
                complemented_metadata[index[i]].column = coordinates[i][1]

        return complemented_metadata

    def build_filenames_for_extracted_images(self, metadata,
                                             image_file_format_string):
        '''
        Build unique filenames for the extracted images based on a format
        string  the extracted metadata.

        Since the number of extracted images may be different than the number
        of uploaded image files (because each image file can contain several
        planes), we have to come up with names for the corresponding files.

        Parameters
        ----------
        List[ChannelImageMetadata]
            metadata

        Returns
        -------
        List[ChannelImageMetadata]
            metadata, where "filename" attribute has been set

        See also
        --------
        `tmt.cfg`_
        '''
        complemented_metadata = list(metadata)
        required_attributes = {'well', 'site', 'row', 'column', 'channel'}
        for md in complemented_metadata:
            if not all([hasattr(md, a) for a in required_attributes]):
                raise MetadataError('Filenames cannot be build because '
                                    'required information is missing: %s' % a)
            md.name = image_file_format_string.format(
                                    cycle=md.cycle, well=md.well,
                                    site=md.site, row=md.row, column=md.column,
                                    channel=md.channel)
        return complemented_metadata
