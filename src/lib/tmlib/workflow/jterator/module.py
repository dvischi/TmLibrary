import os
import sys
import re
import logging
# import imp
import collections
import importlib
import traceback
import numpy as np
import rpy2.robjects
from rpy2.robjects import numpy2ri
from rpy2.robjects import pandas2ri
from rpy2.robjects.packages import importr
from cStringIO import StringIO


from tmlib.workflow.jterator.utils import determine_language
from tmlib.workflow.jterator import handles as hdls
from tmlib.errors import PipelineRunError

logger = logging.getLogger(__name__)


class CaptureOutput(dict):
    '''Class for capturing standard output and error and storing the strings
    in dictionary.

    Examples
    --------
    with CaptureOutput() as output:
        foo()

    Warning
    -------
    Using this approach screws up debugger break points.
    '''
    def __enter__(self):
        self._stdout = sys.stdout
        self._stderr = sys.stderr
        sys.stdout = self._stringio_out = StringIO()
        sys.stderr = self._stringio_err = StringIO()
        return self

    def __exit__(self, *args):
        sys.stdout = self._stdout
        sys.stderr = self._stderr
        output = self._stringio_out.getvalue()
        error = self._stringio_err.getvalue()
        self.update({'stdout': output, 'stderr': error})


class ImageAnalysisModule(object):

    '''Class for a Jterator module, the building block of an image analysis
    pipeline.
    '''

    def __init__(self, name, source_file, description):
        '''
        Parameters
        ----------
        name: str
            name of the module
        source_file: str
            path to program file that should be executed
        description: Dict[str, List[dict]]
            description of module input/output as provided by `handles`
        '''
        self.name = name
        self.source_file = source_file
        self.description = description
        self.outputs = dict()
        self.persistent_store = dict()

    def instantiate_handles(self):
        '''Instantiates a handle for each described parameter.'''
        self.handles = dict()
        self.handles['input'] = list()
        for item in self.description['input']:
            self.handles['input'].append(hdls.create_handle(**item))
        self.handles['output'] = list()
        for item in self.description['output']:
            self.handles['output'].append(hdls.create_handle(**item))

    def build_figure_filename(self, figures_dir, job_id):
        '''Builds name of figure file into which module will write figure
        output of the current job.

        Parameters
        ----------
        figures_dir: str
            path to directory for figure output
        job_id: int
            one-based job index

        Returns
        -------
        str
            absolute path to the figure file
        '''
        return os.path.join(
            figures_dir, '%s_%.5d.json' % (self.name, job_id)
        )

    @property
    def keyword_arguments(self):
        '''dict: name and value of each input handle as key-value pairs'''
        kwargs = collections.OrderedDict()
        for handle in self.handles['input']:
            kwargs[handle.name] = handle.value
        return kwargs

    @property
    def language(self):
        '''str: language of the module (e.g. "python")'''
        return determine_language(self.source_file)

    def _exec_m_module(self, engine):
        logger.debug(
            'adding module source file to Matlab path: "%s"',self.source_file
        )
        # engine.eval('addpath(\'{0}\');'.format(os.path.dirname(self.source_file)))
        module_name = os.path.splitext(os.path.basename(self.source_file))[0]
        engine.eval('import \'jtmodules.{0}\''.format(module_name))
        function_call_format_string = \
            '[{outputs}] = jtmodules.{name}.main({inputs});'
        kwargs = self.keyword_arguments
        logger.debug(
            'evaluating Matlab function with INPUTS: "%s"',
            '", "'.join(kwargs.keys())
        )
        output_names = [handle.name for handle in self.handles['output']]
        func_call_string = function_call_format_string.format(
            outputs=', '.join(output_names),
            name=module_name,
            inputs=', '.join(kwargs.keys())
        )
        # Add arguments as variable in Matlab session
        for name, value in kwargs.iteritems():
            engine.put(name, value)
        # Evaluate the function call
        # NOTE: Unfortunately, the matlab_wrapper engine doesn't return
        # standard output and error (exceptions are caught, though).
        # TODO: log to file
        engine.eval(func_call_string)

        for handle in self.handles['output']:
            val = engine.get('%s' % handle.name)
            if isinstance(val, np.ndarray):
                # Matlab returns arrays in Fortran order
                handle.value = val.copy(order='C')
            else:
                handle.value = val

        return self.handles['output']

    def _exec_py_module(self):
        logger.debug('importing Python module: "%s"' % self.source_file)
        module_name = os.path.splitext(os.path.basename(self.source_file))[0]
        try:
            import jtmodules
        except ImportError:
            raise ImportError(
                'Package "jtmodules" is not installed. '
                'See https://github.com/TissueMAPS/JtModules'
            )
        try:
            module = importlib.import_module('jtmodules.%s' % module_name)
        except ImportError as err:
            raise ImportError(
                'Import of module "%s" failed:\n%s' % (module_name, str(err))
            )
        func = getattr(module, 'main', None)
        if func is None:
            raise PipelineRunError(
                'Module source file "%s" must contain a "main" function.'
                % module_name
            )
        kwargs = self.keyword_arguments
        logger.debug(
            'evaluating Python function with INPUTS: "%s"',
            '", "'.join(kwargs.keys())
        )
        py_out = func(**kwargs)
        # TODO: We could import the output class and check for its type.
        if not isinstance(py_out, tuple):
            raise PipelineRunError(
                'Module "%s" must return an object of type tuple.' % self.name
            )

        # Modules return a namedtuple.
        for handle in self.handles['output']:
            if not hasattr(py_out, handle.name):
                raise PipelineRunError(
                    'Module "%s" didn\'t return output argument "%s".'
                    % (self.name, handle.name)
                )
            handle.value = getattr(py_out, handle.name)

        return self.handles['output']

    def _exec_r_module(self):
        logger.debug('sourcing module: "%s"' % self.source_file)
        # rpy2.robjects.r('source("{0}")'.format(self.source_file))
        module_name = os.path.splitext(os.path.basename(self.source_file))[0]
        rpackage = importr('jtmodules')
        module = getattr(rpackage, module_name)
        func = module.get('main')
        numpy2ri.activate()   # enables use of numpy arrays
        pandas2ri.activate()  # enable use of pandas data frames
        # func = rpy2.robjects.globalenv['main']
        kwargs = self.keyword_arguments
        logger.debug(
            'evaluating R function with INPUTS: "%s"',
            '", "'.join(kwargs.keys())
        )
        # R doesn't have unsigned integer types
        for k, v in kwargs.iteritems():
            if isinstance(v, np.ndarray):
                if v.dtype == np.uint16 or v.dtype == np.uint8:
                    logging.debug(
                        'module "%s" input argument "%s": '
                        'convert unsigned integer data type to integer',
                        self.name, k
                    )
                    kwargs[k] = v.astype(int)
            # TODO: we may have to translate pandas data frames into the
            # R equivalent
            # pd.com.convert_to_r_dataframe(v)
        args = rpy2.robjects.ListVector({k: v for k, v in kwargs.iteritems()})
        base = importr('base')
        r_out = base.do_call(func, args)

        for handle in self.handles['output']:
            # NOTE: R functions are supposed to return a list. Therefore
            # we can extract the output argument using rx2(name).
            # The R equivalent would be indexing the list with "[[name]]".
            if isinstance(r_out.rx2(handle.name), rpy2.robjects.vectors.DataFrame):
                # handle.value = pd.DataFrame(r_var.rx2(name))
                handle.value = rpy2.robjects.pandas2ri(r_out.rx2(handle.name))
            else:
                # handle.value = np.array(r_var.rx2(name))
                handle.value = rpy2.robjects.numpy2ri(r_out.rx2(handle.name))

        return self.handles['output']

    def update_handles(self, store, headless=True):
        '''Updates values of handles that define the arguments of the
        module function.

        Parameters
        ----------
        store: dict
            in-memory key-value store
        headless: bool, optional
            whether plotting should be disabled (default: ``True``)

        Returns
        -------
        List[tmlib.jterator.handles.Handle]
            handles for input keyword arguments

        Note
        ----
        This method must be called BEFORE calling
        :method:`tmlib.jterator.module.Module.run`.
        '''
        for handle in self.handles['input']:
            if isinstance(handle, hdls.PipeHandle):
                try:
                    handle.value = store['pipe'][handle.key]
                except KeyError:
                    raise PipelineRunError(
                        'Value for argument "%s" was not created upstream '
                        'in the pipeline: %s' % (self.name, handle.key)
                    )
                except Exception:
                    raise
            elif isinstance(handle, hdls.Plot) and headless:
                # Overwrite to enforce headless mode if required.
                handle.value = False
        return self.handles['input']

    def _get_reference_objects_name(self, handle):
        '''Determines the name of the segmented objects that are referenced by
        a `Features` handle.

        Parameters
        ----------
        handle: tmlib.workflow.jterator.handle.Features
            output handle with a `objects_ref` attribute, which provides a
            reference to an input handle

        Returns
        -------
        str
            name of the referenced segmented objects
        '''
        objects_names = [
            h.key for h in self.handles['input']
            if h.name == handle.objects_ref and
            isinstance(h, hdls.SegmentedObjects)
        ]
        if len(objects_names) == 0:
            raise PipelineRunError(
                'Invalid object reference for "%s" in module "%s": %s'
                % (handle.name, self.name, handle.objects_ref)
            )
        return objects_names[0]

    def _get_reference_channel_name(self, handle):
        '''Determines the name of the channel that is referenced by a
        `Features` handle.

        Parameters
        ----------
        handle: tmlib.workflow.jterator.handle.Features
            output handle with a `channel_ref` attribute, which provides a
            reference to an input handle

        Returns
        -------
        str
            name of the referenced channel
        '''
        if handle.channel_ref is None:
            return None
        channel_names = [
            h.key for h in self.handles['input']
            if h.name == handle.channel_ref and
            isinstance(h, hdls.IntensityImage)
        ]
        if len(channel_names) == 0:
            raise PipelineRunError(
                'Invalid channel reference for "%s" in module "%s": %s'
                % (handle.name, self.name, handle.channel_ref)
            )
        return channel_names[0]

    def update_store(self, store):
        '''Updates `store` with key-value pairs that were returned by the
        module function.

        Parameters
        ----------
        store: dict
            in-memory key-value store

        Returns
        -------
        store: dict
            updated in-memory key-value store

        Note
        ----
        This method must be called AFTER calling
        :method:`tmlib.jterator.module.Module.run`.
        '''
        for i, handle in enumerate(self.handles['output']):
            if isinstance(handle, hdls.Figure):
                store['current_figure'] = handle.value
            elif isinstance(handle, hdls.SegmentedObjects):
                store['segmented_objects'][handle.key] = handle
                store['pipe'][handle.key] = handle.value
            elif isinstance(handle, hdls.Measurement):
                object_name = self._get_reference_objects_name(handle)
                channel_name = self._get_reference_channel_name(handle)
                if channel_name is not None:
                    new_names = list()
                    for name in handle.value[0].columns:
                        new_names.append('%s_%s' % (name, channel_name))
                    for t in range(len(handle.value)):
                        handle.value[t].columns = new_names
                obj_handle = store['segmented_objects'][object_name]
                obj_handle.add_measurement(handle)
            elif isinstance(handle, hdls.Attribute):
                object_name = self._get_reference_objects_name(handle)
                obj_handle = store['segmented_objects'][object_name]
                obj_handle.add_attribute(handle)
            else:
                store['pipe'][handle.key] = handle.value
        return store

    def run(self, engine=None):
        '''Executes a module, i.e. evaluate the corresponding function with
        the keyword arguments provided by
        :class:`tmlib.workflow.jterator.handles`.

        Parameters
        ----------
        engine: matlab_wrapper.matlab_session.MatlabSession, optional
            engine for non-Python languages, such as Matlab (default: ``None``)

        Note
        ----
        Call :method:`tmlib.jterator.module.Module.update_handles` before
        calling this method and
        :method:`tmlib.jterator.module.Module.update_store` afterwards.
        '''
        if self.language == 'Python':
            return self._exec_py_module()
        elif self.language == 'Matlab':
            return self._exec_m_module(engine)
        elif self.language == 'R':
            return self._exec_r_module()
        else:
            raise PipelineRunError('Language not supported.')

    def __str__(self):
        return (
            '<%s(name=%r, source=%r)>'
            % (self.__class__.__name__, self.name, self.source_file)
        )
