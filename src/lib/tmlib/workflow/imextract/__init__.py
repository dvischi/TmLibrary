from tmlib import __version__


__fullname__ = 'Extration of pixel data from image files'

__description__ = '''Extracts pixel elements from heterogeneous
    microscopy image file formats based on the configured image metadata
    and stores them in a standardized file format.
'''

__logo__ = '''
  _               _               _
 (_)_ __  _____ _| |_ _ _ __ _ __| |_      {name} ({version})
 | | '  \/ -_) \ /  _| '_/ _` / _|  _|     {fullname}
 |_|_|_|_\___/_\_\\\__|_| \__,_\__|\__|     https://github.com/TissueMAPS/TmLibrary
'''.format(name=__name__, version=__version__, fullname=__fullname__)
