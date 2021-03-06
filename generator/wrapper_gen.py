#*******************************************************************************
# Copyright 2014-2018 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#******************************************************************************/

###############################################################################
# The code generator.
# We define jinja2 templates to generate code for all the DAAL algorithms and their
# Result and Model objects. Most macros work on one namespace and expect expect
# values/variables in their env. If not specificied otherwise, we assume we can
# use the following
#          {{ns}}:            current C++ namespace
#          {{algo}}:          algo name (as seen in API)
#          {{args_decl}}:     non-template arguments for wrapper function
#          {{args_call}}:     non-template arguments to pass on
#          {{input_args}}:    args for setting input
#          {{template_decl}}: template parameters for declaration
#          {{template_args}}: template arguments mapping to their possible values
#          {{params_req}}:    dict of required parameters and their values
#          {{params_opt}}:    dict of parameters and their values
#          {{params_get}}:    parameter struct getter
#          {{step_specs}}:    distributed spec
#          {{map_result}}:    Result enum id for getting partial result (can be FULLPARTIAL)
#          {{iface}}:         Interface manager
#          {{result_map}}:    Type information about result type of given algo
#
# The code here is of course highly dependent on manual code that can be found in ../src.
# For example, we pretend we know how distributed algorithm interfaces look like and work.
#
# Also, we use some functions which accept PyObject* to convert the python objects manually
# Similarly, we create PyObjects manually in some C++ functions (e.g. nd-arrays).
###############################################################################
# FIXME remove remaining args/code for distributed computation if none available
# FIXME a revision from scratch would be helpful...

import jinja2
from collections import OrderedDict
from pprint import pprint
import re
from .wrappers import hpat_types

###############################################################################
# generic utility functions/defs needed by generated code
cython_header = '''
# distutils: language = c++
#cython: language_level=2

# Import the Python-level symbols of numpy
import numpy as np

# Import the C-level symbols of numpy
cimport numpy as npc
# import std::string support
from libcpp.string cimport string as std_string
from libcpp cimport bool
from cpython.ref cimport PyObject
from cython.operator cimport dereference as deref
from libc.stdint cimport int64_t

try:
    import pandas
    pdDataFrame = pandas.DataFrame
    pdSeries = pandas.Series
except:
    class pdDataFrame:
        pass
    class pdSeries:
        pass

npc.import_array()

hpat_spec = []

cdef extern from "daal4py_cpp.h":
    cdef cppclass NumericTablePtr:
        pass

    cdef cppclass data_management_DataCollectionPtr:
        pass

    ctypedef NumericTablePtr data_management_NumericTablePtr


cdef extern from "pickling.h":
    cdef object serialize_si(void *) nogil
    cdef T* deserialize_si[T](object) nogil


cpdef _rebuild(class_constructor, state_data):
    cls = class_constructor()
    cls.__setstate__(state_data)
    return cls


cdef class data_management_datacollection:
    cdef data_management_DataCollectionPtr * c_ptr

    def __dealloc__(self):
        del self.c_ptr


cdef extern from "daal4py.h":
    cdef const double NaN64
    cdef const float  NaN32

    cdef cppclass table_or_flist :
        table_or_flist(PyObject *) except +

    cdef cppclass dict_NumericTablePtr:
        pass

    cdef std_string to_std_string(PyObject * o) except +

    cdef PyObject * make_nda(NumericTablePtr * nt_ptr) except +
    cdef PyObject * make_nda(dict_NumericTablePtr * nt_ptr, void *) except +
    cdef NumericTablePtr * make_nt(PyObject * nda) except +
    cdef dict_NumericTablePtr * make_dnt(PyObject * nda, void *) except +

    cdef T* dynamicPointerPtrCast[T,U](U*)
    cdef bool is_valid_ptrptr[T](T * o)

    cdef void * e2s_algorithms_pca_result_dataForTransform
    cdef void * s2e_algorithms_pca_transform

NAN64 = NaN64
NAN32 = NaN32


cdef extern from "daal.h":
    ctypedef unsigned long long DAAL_UINT64


cdef extern from "daal4py_cpp.h":
    cdef void c_daalinit(int nthreads) except +
    cdef void c_daalfini() except +
    cdef size_t c_num_threads() except +
    cdef size_t c_num_procs() except +
    cdef size_t c_my_procid() except +


def daalinit(nthreads = -1):
    c_daalinit(nthreads)

def daalfini():
    c_daalfini()

def num_threads():
    return c_num_threads()

def num_procs():
    return c_num_procs()

def my_procid():
    return c_my_procid()


cdef table_or_flist * mk_table_or_flist(object x):
    if isinstance(x, pdDataFrame):
        x = [x.loc[:,i].values for i in x]
    elif isinstance(x, pdSeries):
        x = [x.values]
    return new table_or_flist(<PyObject *>x)

'''

###############################################################################
# generates result/model classes
# Generates header, c++ and cython code together, separated by "%SNIP%
# requires {{enum_gets}}    list of triplets of members accessed via get(ns, name, type)
#          {{named_gets}}   list of pairs(name, type) of members accessed via type var = getName()
#          {{class_type}}   C++ class
typemap_wrapper_template = """
{% set flatname = (class_type|flat|strip(' *')|lower).replace('ptr', '') %}
{% set splitname = class_type.rsplit('::', 1) %}
typedef {{class_type}} {{class_type|flat|strip(' *')}};
{% if enum_gets or named_gets %}
{% for m in enum_gets %}
extern "C" {{m[2]}} {{'*' if 'Ptr' in m[2] else ''}} get_{{flatname}}_{{m[1]}}({{class_type}} * obj_);
{% endfor %}
{% for m in named_gets %}
extern "C" {{m[0]}} {{'*' if 'Ptr' in m[0] else ''}} get_{{flatname}}_{{m[1]}}({{class_type}} * obj_);
{% endfor %}
{% for m in get_methods %}
extern "C" {{m[0]}} * get_{{flatname}}_{{m[1]}}({{class_type}} *, {{m[2]}});
{% endfor %}
{% if (flatname.startswith('gbt_') or flatname.startswith('decision_forest')) and flatname.endswith('model') %}
// FIXME
extern "C" size_t get_{{flatname}}_numberOfTrees({{class_type}} * obj_);
{% endif %}
%SNIP%
{% for m in enum_gets %}
extern "C" {{m[2]}} {{'*' if 'Ptr' in m[2] else ''}} get_{{flatname}}_{{m[1]}}({{class_type}} * obj_)
{
    return RAW< {{m[2]}} >()((*obj_)->get(daal::{{m[0]}}::{{m[1]}}));
}
{% endfor %}
{% for m in named_gets %}
extern "C" {{m[0]}} {{'*' if 'Ptr' in m[0] else ''}} get_{{flatname}}_{{m[1]}}({{class_type}} * obj_)
{
    return RAW< {{m[0]}} >()((*obj_)->get{{m[1]}}());
}
{% endfor %}
{% for m in get_methods %}
extern "C" {{m[0]}} * get_{{flatname}}_{{m[1]}}({{class_type}} * obj_, {{m[2]}} {{m[3]}})
{
    return new {{m[0]}}((*obj_)->get{{m[1]}}({{m[3]}}));
}
{% endfor %}
{% if (flatname.startswith('gbt_') or flatname.startswith('decision_forest')) and flatname.endswith('model') %}
// FIXME
extern "C" size_t get_{{flatname}}_numberOfTrees({{class_type}} * obj_)
{
    return (*obj_)->numberOfTrees();
}
{% endif %}

%SNIP%
cdef extern from "daal4py_cpp.h":
    cdef cppclass {{class_type|flat|strip(' *')}}:
        pass

{% if not class_type.startswith('daal::'+ns) %}
{% endif %}
cdef extern from "daal4py_cpp.h":
{% for m in enum_gets %}
    cdef {{m[2]|d2cy}} get_{{flatname}}_{{m[1]}}({{class_type|flat}} obj_) except +
{% endfor %}
{% for m in named_gets %}
    cdef {{m[0]|d2cy}} get_{{flatname}}_{{m[1]}}({{class_type|flat}} obj_) except +
{% endfor %}
{% for m in get_methods %}
    cdef {{(m[0]|d2cy)}} get_{{flatname}}_{{m[1]}}({{class_type|flat}} obj_, {{m[2]}} {{m[3]}}) except +
{% endfor %}
{% if (flatname.startswith('gbt_') or flatname.startswith('decision_forest')) and flatname.endswith('model') %}
    # FIXME
    cdef size_t get_{{flatname}}_numberOfTrees({{class_type|flat}} obj_) except +
{% endif %}

cdef class {{flatname}}:
    '''
    Properties:
    '''
    cdef {{class_type|flat}} c_ptr

    def __cinit__(self):
        self.c_ptr = NULL
        pass

    def __dealloc__(self):
        del self.c_ptr

    def __init__(self, int64_t ptr=0):
        self.c_ptr = <{{class_type|flat}}>ptr

{% for m in enum_gets+named_gets %}
{% set rtype = m[2]|d2cy(False) if m in enum_gets else m[0]|d2cy(False) %}

    @property
    def {{m[1]}}(self):
{% if ('Ptr' in rtype and 'NumericTablePtr' not in rtype) or '__iface__' in rtype %}
{% set frtype=(rtype.strip(' *&')|flat(False)|strip(' *')).replace('Ptr', '')|lower %}
        ':type: {{frtype}}'
        if not is_valid_ptrptr(self.c_ptr):
            raise ValueError("Pointer to DAAL entity is NULL")
        cdef {{frtype}} res = {{frtype}}.__new__({{frtype}})
        res.c_ptr = get_{{flatname}}_{{m[1]}}(self.c_ptr)
        return res
{% else %}
        ':type: {{'Numpy array' if 'NumericTablePtr' in rtype else rtype}}'
        if not is_valid_ptrptr(self.c_ptr):
            raise ValueError("Pointer to DAAL entity is NULL")
        res = get_{{flatname}}_{{m[1]}}(self.c_ptr)
{% if 'NumericTablePtr' in rtype %}
        return {{'<object>make_nda(res, e2s_algorithms_'+flatname+'_'+m[1]+')' if 'dict_NumericTablePtr' in rtype else '<object>make_nda(res)'}}
{% else %}
        return res
{% endif %}
{% endif %}
{% endfor %}

{% if (flatname.startswith('gbt_') or flatname.startswith('decision_forest')) and flatname.endswith('model') %}
    @property
    def NumberOfTrees(self):
        'FIXME'
        if not is_valid_ptrptr(self.c_ptr):
            raise ValueError("Pointer to DAAL entity is NULL")
        return get_{{flatname}}_numberOfTrees(self.c_ptr)
{% endif %}

    cdef _get_most_derived(self):
{% if derived %}
{% for m in derived %}
{% set dertype = m|d2cy %}
{% set cytype = m.replace('Ptr', '')|d2cy(False)|lower %}
        cdef {{m|d2cy}} tmp_ptr{{loop.index}} = dynamicPointerPtrCast[{{m|d2cy(False)}}, {{class_type|flat(False)}}](self.c_ptr)
        cdef {{cytype}} res{{loop.index}}
        if tmp_ptr{{loop.index}}:
            res{{loop.index}} = {{cytype}}.__new__({{cytype}})
            res{{loop.index}}.c_ptr = tmp_ptr{{loop.index}}
            return res{{loop.index}}
{% endfor %}
{% endif %}
        return self

{% for m in get_methods %}
{% set frtype = m[0].replace('Ptr', '')|d2cy(False)|lower %}
    def {{m[1]|d2cy(False)}}(self, {{m[2]|d2cy(False)}} {{m[3]}}):
        ':type: {{frtype}} (or derived)'
        if not is_valid_ptrptr(self.c_ptr):
            raise ValueError("Pointer to DAAL entity is NULL")
{% if 'Ptr' in m[0] %}
        cdef {{frtype}} res = {{frtype}}.__new__({{frtype}})
        res.c_ptr = get_{{flatname}}_{{m[1]}}(self.c_ptr, {{m[3]}})
{% if '_model' in frtype %}
        return res._get_most_derived()
{% else %}
        return res
{% endif %}
{% else %}
        return get_{{flatname}}_{{m[1]}}(self.c_ptr, {{m[3]}})
{% endif %}
{% endfor %}

    def __setstate__(self, state):
        if isinstance(state, bytes):
           self.c_ptr = deserialize_si[{{class_type|flat|strip(' *')}}](state)
        else:
           raise ValueError("Invalid state .....")

    def __getstate__(self):
        if self.c_ptr == NULL:
            raise ValueError("Pointer to DAAL entity is NULL")
        bytes = serialize_si(self.c_ptr)
        return bytes

    def __reduce__(self):
        state_data = self.__getstate__()
        return (_rebuild, (self.__class__, state_data,))


cdef api void * unbox_{{flatname}}(a):
    return (<{{flatname}}>a).c_ptr


hpat_spec.append({
    'pyclass': {{flatname}},
    'c_name' : '{{flatname}}',
    'attrs'  : [
{% for m in enum_gets+named_gets %}
                ('{{m[1]}}', '{{m[1]|d2hpat(m[2], flatname) if m in enum_gets else m[1]|d2hpat(m[0], flatname)}}'),
{% endfor %}
]})
{% else %}
%SNIP%
%SNIP%
{% endif %}
{% if not class_type.startswith('daal::'+ns) %}
cdef extern from "daal4py_cpp.h":
    ctypedef {{class_type|flat(False)}} {{(ns+'::'+splitname[-1])|flat|strip(' *')}}

{% set alias = (ns+'::'+splitname[-1]).replace('Ptr', '')|flat|lower|strip(' *') %}
{% set actual = flatname %}
{{alias}} = {{actual}}

hpat_spec.append({
    'c_name' : '{{alias}}',
    'alias' : '{{actual}}',
})
{% endif %}
"""

# macro generating C++ class for DAAL interface classes
# accepts interface name and C++ type
gen_cpp_iface_macro = """
{% macro gen_cpp_iface(iface_name, iface_type) %}
class {{iface_name}}__iface__ : public {{parent|d2cy(False) if parent else 'algo_manager__iface__'}}
{
public:
    typedef {{iface_type}} daal_type;
    typedef {{iface_type}} {{iface_name}}Ptr_type;
    virtual {{iface_name}}Ptr_type get_ptr() = 0; //{return {{iface_name}}Ptr_type();}
};

typedef {{iface_name}}__iface__ c_{{iface_name}}__iface__;

static {{iface_type}} to_daal(c_{{iface_name}}__iface__ * t) {return t ? t->get_ptr() : {{iface_type}}();}
{% endmacro %}
"""

# macro generating cython class for DAAL interface classes
# accepts interface name and C++ type
gen_cython_iface_macro = """
{% macro gen_cython_iface(iface_name, iface_type) %}
cdef extern from "daal4py_cpp.h":
    cdef cppclass c_{{iface_name}}__iface__{{'(c_'+parent|d2cy(False)+')' if parent else ''}}:
        pass

#    ctypedef c_{{iface_name}}__iface__ c_{{iface_type|flat|strip(' *')}};

{% set inl = iface_name|lower + '__iface__' %}
{% if parent %}
cdef class {{inl}}({{parent|d2cy(False)|lower}}):
    pass
{% else %}
cdef class {{inl}}():
    cdef c_{{iface_name}}__iface__ * c_ptr

    def __cinit__(self):
        self.c_ptr = NULL

    def __dealloc__(self):
        del self.c_ptr
{% endif %}

hpat_spec.append({
    'pyclass'     : {{inl}},
    'c_name'      : '{{inl}}',
})
{% endmacro %}
"""

# macro generating typedefs in manager classes, e.g. algo and result types
# note that we will have multiple result types in dist-mode: each step has its own
gen_typedefs_macro = """
{% macro gen_typedefs(ns, template_decl, template_args, mode="Batch", suffix="b", step_spec=None) %}
{% set disttarg = (step_spec.name.rsplit('__', 1)[0] + ', ') if step_spec.name else "" %}
{% if template_decl|length > 0  %}
    typedef daal::{{ns}}::{{mode}}<{{disttarg + template_args|fmt('{}', 'value')}}> algo{{suffix}}_type;
{% else %}
    typedef daal::{{ns}}::{{mode}} algo{{suffix}}_type;
{% endif %}
{% if step_spec %}
    typedef {{step_spec.iomanager}}< algo{{suffix}}_type, {{', '.join(step_spec.input)}}, {{step_spec.output}}{{(","+",".join(step_spec.iomargs)) if step_spec.iomargs else ""}} > iom{{suffix}}_type;
{% else %}
{% if iombatch %}
    typedef {{iombatch}} iom{{suffix}}_type;
{% else %}
    typedef IOManager< algo{{suffix}}_type, daal::services::SharedPtr< typename algo{{suffix}}_type::InputType >, daal::services::SharedPtr< typename algo{{suffix}}_type::ResultType > > iom{{suffix}}_type;
{% endif %}
{% endif %}
{%- endmacro %}
"""

# macro for generate an algorithm instance with name algo$suffix
# This can also be used for steps of distributed mode
# set member=True if you want a init a member var (shared pointer)
gen_inst_algo = """
{% macro gen_inst(ns, params_req, params_opt, params_get, create, suffix="", step_spec=None, member=False) %}
{% set algo = 'algo' + suffix %}
{% if step_spec.construct %}
{% set ctor = create + '(' + step_spec.construct + ')' %}
{% elif create  %}
{% set ctor = ('::create(_' + ', _'.join(create.keys()) + ')').replace('(_)', '()') %}
{% else %}
{% set ctor = '(' + params_req|fmt('to_daal({})', 'arg_member', sep=', ') + ')' %}
{% endif %}
{% if member %}
_algo{{suffix}}{{' = (' if create else '.reset(new '}}{{algo}}_type{{ctor}}); // llllll
{% elif create %}
auto {{algo}} = {{algo}}_type{{ctor}}; // llllll
{% else %}
auto {{algo}}_obj = {{algo}}_type{{ctor}}; // llllll
        {{algo}}_type * {{algo}} = &{{algo}}_obj;
{% endif %}
{% if (step_spec == None or step_spec.params) and params_get and params_opt|length and not create %}
        init_parameters({{('_' if member else '')+algo}}->{{params_get}});
{% else %}
        // skipping parameter initialization
{% endif %}
{%- endmacro %}
"""

# macro to generate the body of a compute function (batch and distributed)
gen_compute_macro = gen_inst_algo + """
{% macro gen_compute(ns, input_args, params_req, params_opt, suffix="", step_spec=None, tonative=True, iomtype=None, setupmode=False) %}
{% set iom = iomtype if iomtype else "iom"+suffix+"_type" %}
{% if step_spec %}
{% if step_spec.addinput %}
(const std::vector< typename {{iom}}::input1_type > & input{{', ' + step_spec.extrainput if step_spec.extrainput else ''}})
    {
        {{gen_inst(ns, params_req, params_opt, params_get, create, suffix, step_spec)}}
        int nr = 0, i = 0;
        for(auto data = input.begin(); data != input.end(); ++data, ++i) {
            if(*data) {
                algo{{suffix}}->input.add({{step_spec.addinput}}, to_daal(*data));
                ++nr;
            }
        }
        if(nr == 0 ) return typename {{iom}}::result_type();
{% else %}
({% for ia in step_spec.input %}const typename {{iom}}::input{{loop.index}}_type & input{{loop.index}}{{'' if loop.last else ', '}}{% endfor %}{{', ' + step_spec.extrainput if step_spec.extrainput else ''}})
    {
        {{gen_inst(ns, params_req, params_opt, params_get, create, suffix, step_spec)}}
{% for ia in step_spec.input %}
        if(input{{loop.index}}) algo{{suffix}}->input.set({{step_spec.setinput[loop.index0]}}, to_daal(input{{loop.index}}));
{% endfor %}
{% endif %}
{% if step_spec.staticinput %}{% for ia in step_spec.staticinput %}
        if(! use_default(_{{ia[1]}})) algo{{suffix}}->input.set({{ia[0]}}, to_daal(_{{ia[1]}}));
{% endfor %}{% endif %}
{% else %}
{% if setupmode %}
(bool setup_only = false)
{% else %}
()
{% endif %}
    {
        ThreadAllow _allow_;
        auto algo{{suffix}} = _algo{{suffix}};

{% for ia in input_args %}
{% if "table_or_flist" in ia.typ_cpp %}
        if(!{{ia.arg_member}}->table && {{ia.arg_member}}->file.size()) {{ia.arg_member}}->table = readCSV({{ia.arg_member}}->file);
        if({{ia.arg_member}}->table) algo{{suffix}}->input.set({{ia.value}}, {{ia.arg_member}}->table);
{% else %}
        if({{ia.arg_member}}) algo{{suffix}}->input.set({{ia.value}}, to_daal({{ia.arg_member}}));
{% endif %}
{% endfor %}
{% if setupmode %}

        if(setup_only) return typename iomb_type::result_type();
{% endif %}
{% endif %}

        algo{{suffix}}->compute();
{% if step_spec %}
        if({{iom}}::needsFini()) {
            algo{{suffix}}->finalizeCompute();
        }
{% endif %}
{% if tonative %}
        typename iomb_type::result_type daalres({{iom}}::getResult(*algo{{suffix}});
        int gc = 0;
{% if not step_spec %}        _allow.disallow();
{% endif %}
        NTYPE res = native_type(daalres, gc);
        TMGC(gc);
        return res;
{% else %}
        return {{iom}}::getResult(*algo{{suffix}});
{% endif %}
    }
{%- endmacro %}
"""

# generates the de-templetized *__iface__ struct with providing generic compute(...)
algo_iface_template = """
{% set prnt = iface[0]+'__iface__' if iface[0] else 'algo_manager__iface__' %}
struct {{algo}}__iface__ : public {{prnt}}
{
    {{distributed.decl_member}};
    {{streaming.decl_member}};
    {{algo}}__iface__({{[distributed, streaming]|fmt('{}', 'decl_dflt_cpp')}})
        : {{prnt}}()
          {{[distributed, streaming]|fmt(',{}', 'init_member', sep='\n')|indent(10) if distributed.name or streaming.name else '\n'}}
    {}
{% set indent = 23+(result_map.class_type|length) %}
    virtual {{result_map.class_type}} * compute({{input_args|fmt('{}', 'decl_cpp', sep=',\n')|indent(indent)}},
{{' '*indent}}bool setup_only = false)
        {assert(false); return NULL;}
{% if streaming.name %}
    virtual {{result_map.class_type}} * finalize() {assert(false); return NULL;}
{% endif %}
};
"""

# generates "manager" class for managing distributed and batch modes of a given algo
manager_wrapper_template = gen_typedefs_macro + gen_compute_macro + """
{% if template_decl|length == template_args|length %}
// The type used in cython
typedef {{algo}}__iface__  c_{{algo}}_manager__iface__;

// The algo creation function
extern "C" {{algo}}__iface__ * mk_{{algo}}({{params_all|fmt('{}', 'decl_cpp', sep=',\n')|indent(27+2*(algo|length))}});
{% endif %}

{% if template_decl  %}
template<{% for x in template_decl %}{{template_decl[x]['template_decl'] + ' ' + x + ('' if loop.last else ', ')}}{% endfor %}>
{% endif %}
struct {{algo}}_manager{% if template_decl|length != template_args|length %}<{{template_args|fmt('{}', 'value')}}>{% endif %} : public {{algo}}__iface__
{% if not incomplete %}
{
{{gen_typedefs(ns, template_decl, template_args, mode="Batch")}}
    {{args_all|fmt('{}', 'decl_member', sep=';\n')|indent(4)}};
    daal::services::SharedPtr< algob_type > _algob;

{% if streaming.name %}
{{gen_typedefs(ns, template_decl, template_args, mode="Online", suffix="stream")}}
    daal::services::SharedPtr< algostream_type > _algostream;
{% endif %}

    {{algo}}_manager({{params_ds|fmt('{}', 'decl_cpp', sep=',\n')|indent(13+algo|length)}})
        : {{algo}}__iface__({{[distributed, streaming]|fmt('{}', 'arg_cpp')}})
          {{args_all|fmt(',{}', 'init_member', sep='\n')|indent(10)}}
          , _algob()
{% if streaming.name %}
          , _algostream()
{% endif %}
    {
{% if streaming.name %}
      if({{streaming.arg_member}}) {
        {{gen_inst(ns, params_req, params_opt, params_get, create, suffix="stream", member=True)}}
      } else
{% endif %}
      {
        {{gen_inst(ns, params_req, params_opt, params_get, create, suffix="b", member=True)}}
      }
    }
    ~{{algo}}_manager()
    {
{% for i in args_decl %}
{% if 'table_or_flist' in i %}
        delete _{{args_call[loop.index0]}};
{% elif '*' in i %}
        // ?? delete _{{args_call[loop.index0]}};
{% endif %}
{% endfor %}
    }

private:
{% if params_opt|length and not create %}
    template< typename PType >
    void init_parameters(PType & parameter)
    {
        {{params_opt|fmt('if(! use_default({})) parameter.{} = to_daal({});', 'arg_member', 'daalname', 'todaal_member', sep='\n')|indent(8)}}
    }
{% endif %}

{% for ifc in iface if ifc %}
    virtual {{ifc}}__iface__::{{ifc}}Ptr_type get_ptr()
    {
        return _algob;
    }
{% endfor %}

    typename iomb_type::result_type batch{{gen_compute(ns, input_args, params_req, params_opt, suffix="b", iomtype=iombatch, tonative=False, setupmode=True)}}

{% if streaming.name %}
    typename iomb_type::result_type stream{{gen_compute(ns, input_args, params_req, params_opt, suffix="stream", iomtype=iombatch, tonative=False)}}

    typename iomb_type::result_type * finalize()
    {
{% if distributed.name %}
        if({{distributed.arg_member}}) throw std::invalid_argument("finalize() not supported in distributed mode");
{% endif %}
        if({{streaming.arg_member}}) {
            _algostream->finalizeCompute();
            return new typename iomb_type::result_type(_algostream->getResult());
        } else {
            return new typename iomb_type::result_type(_algob->getResult());
        }
    }

{% endif %}
{% if step_specs is defined and distributed.name %}
    // Distributed computing
public:
{% for i in range(step_specs|length) %}
{{gen_typedefs(ns, template_decl, template_args, mode="Distributed", suffix=step_specs[i].name, step_spec=step_specs[i])}}
{% endfor %}

{% for i in range(step_specs|length) %}
{% set sname = "run_"+step_specs[i].name %}
    typename iom{{step_specs[i].name}}_type::result_type {{sname + gen_compute(ns, input_args, params_req, params_opt, suffix=step_specs[i].name, step_spec=step_specs[i], tonative=False)}}

{% endfor %}

{% set inp_names = step_specs[0].inputnames if (step_specs|length > 0) else inputnames %}
    static const int NI = {{inp_names|length}};

private:
    typename iomb_type::result_type distributed()
    {
        return {{pattern}}::{{pattern}}< {{algo}}_manager< {{template_args|fmt('{}', 'name')}} > >::compute(*this, to_daal(_{{'), to_daal(_'.join(inp_names)}}));
    }
{% endif %}

public:
    typename iomb_type::result_type * compute({{input_args|fmt('{}', 'decl_cpp', sep=',\n')|indent(46)}},
                                              bool setup_only = false)
    {
        {{input_args|fmt('{}', 'assign_member', sep=';\n')|indent(8)}};

{% set batchcall = '('+streaming.arg_member+' ? stream() : batch(setup_only))' if streaming.name else 'batch(setup_only)'%}
{% if distributed.name %}
        typename iomb_type::result_type daalres = {{distributed.arg_member + ' ? distributed() : ' + batchcall}};
        return new typename iomb_type::result_type(daalres);
{% else %}
        return new typename iomb_type::result_type({{batchcall}});
{% endif %}
    }
};
{% else %}
{};
{% endif %}
"""

# generates cython class wrappers for given algo
# also generates defs for __iface__ class
parent_wrapper_template = """
cdef extern from "daal4py.h":
    # declare the C++ equivalent of the manager__iface__ class, providing de-templatized access to compute
    cdef cppclass c_{{algo}}_manager__iface__{{'(c_'+iface[0]+'__iface__)' if iface[0] else ''}}:
{% set indent = 17+(result_map.class_type|flat|length) %}
        {{result_map.class_type|flat}} compute({{input_args|fmt('{}', 'decl_cyext', sep=',\n')|indent(indent)}},
{{' '*indent}}const bool setup_only) except +
{% if streaming.name %}
        {{result_map.class_type|flat}} finalize() except +
{% endif %}


cdef extern from "daal4py_cpp.h":
    # declare the C++ construction function. Returns the manager__iface__ for access to de-templatized constructor
    cdef c_{{algo}}_manager__iface__ * mk_{{algo}}({{params_all|fmt('{}', 'decl_cyext', sep=',\n')|indent(35+2*(algo|length))}}) except +

# this is our actual algorithm class for Python
cdef class {{algo}}{{'('+iface[0]|lower+'__iface__)' if iface[0] else ''}}:
    '''
    {{algo}}
    {{params_all|fmt('{}', 'sphinx', sep='\n')|indent(4)}}
    '''
    # Init simply forwards to the C++ construction function
    def __cinit__(self,
                  {{params_all|fmt('{}', 'decl_dflt_cy', sep=',\n')|indent(18)}}):
{% if distributed.name and streaming.name %}
        if {{[distributed, streaming]|fmt('{}', 'arg_py', sep=' and ')}}:
            raise ValueError('distributed streaming not supported')
{% endif %}
        self.c_ptr = mk_{{algo}}({{params_all|fmt('{}', 'arg_cyext', sep=',\n')|indent(25+(algo|length))}})

{% if not iface[0] %}
    # the C++ manager__iface__ (de-templatized)
    cdef c_{{algo}}_manager__iface__ * c_ptr

    def __dealloc__(self):
        del self.c_ptr
{% endif %}

    # compute simply forwards to the C++ de-templatized manager__iface__::compute
    def compute(self,
                {{input_args|fmt('{}', 'decl_cy', sep=',\n')|indent(16)}},
                setup=False):
        if self.c_ptr == NULL:
            raise ValueError("Pointer to DAAL entity is NULL")
{% set cytype = result_map.class_type.replace('Ptr', '')|d2cy(False)|lower %}
        algo = <c_{{algo}}_manager__iface__ *>self.c_ptr
        # we cannot have a constructor accepting a c-pointer, so we split into construction and setting pointer
        cdef {{cytype}} res = {{cytype}}.__new__({{cytype}})
        res.c_ptr = deref(algo).compute({{input_args|fmt('{}', 'arg_cyext', sep=',\n')|indent(40)}},
                                        setup)
        return res

{% if streaming.name %}
    # finalize simply forwards to the C++ de-templatized manager__iface__::finalize
    def finalize(self):
        if self.c_ptr == NULL:
            raise ValueError("Pointer to DAAL entity is NULL")
        algo = <c_{{algo}}_manager__iface__ *>self.c_ptr
        # we cannot have a constructor accepting a c-pointer, so we split into construction and setting pointer
        cdef {{cytype}} res = {{cytype}}.__new__({{cytype}})
        res.c_ptr = deref(algo).finalize()
        return res
{% endif %}

{% if add_setup %}
    # setup forwards to the C++ de-templatized manager__iface__::compute(..., setup_only=true)
    def setup(self,
             {{input_args|fmt('{}', 'decl_cy', sep=',\n')|indent(14)}}):
        if self.c_ptr == NULL:
            raise ValueError("Pointer to DAAL entity is NULL")
        algo = <c_{{algo}}_manager__iface__ *>self.c_ptr
        deref(algo).compute({{input_args|fmt('{}', 'arg_cyext', sep=',\n')|indent(28)}},
                            True)
        return None
{% endif %}
"""

# generates the C++ algorithm construction function
# it all it does is dispatching to the template managers from given arguments
# tfactory is a recursive jinja2 macro to handle any number of template args
algo_wrapper_template = """
{% macro tfactory(tmpl_spec, prefix, params, dist=False, args=[], indent=4) %}
{% for a in tmpl_spec[0][1]['values'] %}
{{" "*indent}}if({{tmpl_spec[0][0]}} == "{{a.rsplit('::',1)[-1]}}") {
{% if tmpl_spec|length == 1 %}
{% set algo_type = prefix + '<' + ', '.join(args+[a]) + ' >' %}
{{" "*(indent+4)}}return new {{algo_type}}({{params|fmt('{}', 'arg_cpp')}});
{% else %}
{{tfactory(tmpl_spec[1:], prefix, params, dist, args+[a], indent+4)}}
{% endif %}
{{" "*(indent)}}} else {% if loop.last %} {
{{" "*(indent+4)}} std::cerr << "Error in {{algo}}: Cannot handle unknown value for parameter '{{tmpl_spec[0][0]}}': '" << {{tmpl_spec[0][0]}} << "'" << std::endl;
{{" "*(indent)}}}
{% endif %}
{% endfor %}
{%- endmacro %}

extern "C" {{algo}}__iface__ * mk_{{algo}}({{params_all|fmt('{}', 'decl_cpp', sep=',\n')|indent(27+2*(algo|length))}})
{
    ThreadAllow _allow_;
{% if template_decl %}
{{tfactory(template_decl.items()|list, algo+'_manager', params_ds, dist=dist)}}
    std::cerr << "Error: Could not construct {{algo}}." << std::endl;
    return NULL;
{% else %}
    return new {{algo}}_manager({{params_ds|fmt('{}', 'arg_cpp')}});
{% endif %}
}

extern "C" void * compute_{{algo}}({{algo}}__iface__ * algo,
{{' '*(27+(algo|length))}}{{input_args|fmt('{}', 'decl_c', sep=',\n')|indent(27+(algo|length))}})
{
{% if distributed.name %}
    algo->{{distributed.arg_member}} = c_num_procs() > 0;
{% endif %}
    void * res = algo->compute({{input_args|fmt('{}', 'arg_c', sep=',\n')|indent(31)}});
    return res;
};
"""

# generate a D4PSpec
hpat_spec_template = '''
hpat_spec.append({
    'pyclass'     : {{algo}},
    'c_name'      : '{{algo}}',
    'params'      : [{{params_all|fmt('{}', 'spec', sep=',\n')|indent(21)}}],
    'input_types' : [{{input_args|fmt('{}', 'spec', sep=',\n')|indent(21)}}],
    'result_dist' : {{"'REP'" if step_specs is defined else "'OneD'"}}
})
'''

##################################################################################
# A set of jinja2 filters to convert arguments, types etc which where extracted
# from DAAL C++ headers to cython syntax and/or C++ for our own code
##################################################################################
def flat(t, cpp=True):
    '''Flatten C++ name, leaving only what's needed to disambiguate names.
       E.g. stripping of leading namespaces and replaceing :: with _
    '''
    def _flat(ty):
        def __flat(typ):
            nn = typ.split('::')
            if nn[0] == 'daal':
                if nn[1] == 'algorithms':
                    r = '_'.join(nn[2:])
                else:
                    r = '_'.join(nn[1:])
            elif nn[0] == 'algorithms':
                r = '_'.join(nn[1:])
            else:
                r = '_'.join(nn)
            return ('c_' if cpp and typ.endswith('__iface__') else '') + r + (' *' if cpp and any(typ.endswith(x) for x in ['__iface__', 'Ptr']) else '')
        ty = ty.replace('daal::algorithms::kernel_function::KernelIfacePtr', 'daal::services::SharedPtr<kernel_function::KernelIface>')
        ty = re.sub(r'(daal::)?(algorithms::)?(engines::)?EnginePtr', r'daal::services::SharedPtr<engines::BatchBase>', ty)
        ty = re.sub(r'(?:daal::)?(?:algorithms::)?([^:]+::)BatchPtr', r'daal::services::SharedPtr<\1Batch>', ty)
        ty = re.sub(r'(daal::)?services::SharedPtr<([^>]+)>', r'\2__iface__', ty)
        return ' '.join([__flat(x).replace('const', '') for x in ty.split(' ')])
    return [_flat(x) for x in t if x] if isinstance(t,list) else _flat(t)

def d2cy(ty, cpp=True):
    def flt(t, cpp):
        return flat(t, cpp).replace('lambda', 'lambda_')
    return [flt(x,cpp) for x in ty if x] if isinstance(ty,list) else flt(ty,cpp)


def d2hpat(arg, ty, fn):
    def flt(arg, t):
        rtype = d2cy(t)
        if fn in hpat_types and arg in hpat_types[fn]:
            return hpat_types[fn][arg]
        return 'dtable_type' if 'NumericTablePtr' in rtype else rtype.replace('ModelPtr', 'model').replace(' ', '')
    return [flt(x,y) for x,y in zip(arg, ty)] if isinstance(ty,list) else flt(arg, ty)

def fmt(*args, **kwargs):
    sep = kwargs['sep'] if 'sep' in kwargs else ', '
    return sep.join([y for y in [x.format(args[1], *args[2:]) for x in args[0]] if y])

jenv = jinja2.Environment(trim_blocks=True)
jenv.filters['match'] = lambda a, x : [x for x in a if s in x]
jenv.filters['d2cy'] = d2cy
jenv.filters['flat'] = flat
jenv.filters['d2hpat'] = d2hpat
jenv.filters['strip'] = lambda s, c : s.strip(c)
jenv.filters['quote'] = lambda x: "'"+x+"'" if x else ''
jenv.filters['fmt'] = fmt

class wrapper_gen(object):
    def __init__(self, ac, ifaces):
        self.algocfg = ac
        self.ifaces = ifaces

    def gen_headers(self):
        """
        return code for initing
        """
        cpp = "#ifndef DAAL4PY_CPP_INC_\n#define DAAL4PY_CPP_INC_\n#include <daal4py_dist.h>\n\ntypedef daal::data_management::interface1::NumericTablePtr NumericTablePtr;"
        pyx = ''
        for i in self.ifaces:
            tstr = gen_cython_iface_macro + '{{gen_cython_iface("' + i + '", "' + self.ifaces[i][0] + '")}}\n'
            t = jenv.from_string(tstr)
            pyx += t.render({'parent': self.ifaces[i][1]}) + '\n'
            tstr = gen_cpp_iface_macro + '{{gen_cpp_iface("' + i + '", "' + self.ifaces[i][0] + '")}}\n'
            t = jenv.from_string(tstr)
            cpp += t.render({'parent': self.ifaces[i][1]}) + '\n'

        return (cpp, cython_header + pyx)


    ##################################################################################
    def gen_modelmaps(self, ns, algo):
        """
        return string from typemap_wrapper_template for given Model.
        uses entries from 'gets' in Model class def to fill 'named_gets'.
        """
        jparams = self.algocfg[ns + '::' + algo]['model_typemap']
        if len(jparams) > 0:
            jparams['ns'] = ns
            jparams['algo'] = algo
            t = jenv.from_string(typemap_wrapper_template)
            return (t.render(**jparams) + '\n').split('%SNIP%')
        return '', '', ''


    ##################################################################################
    def gen_resultmaps(self, ns, algo):
        """
        Generates typedefs for Result type of given namespace.
        Uses target language-specific defines/functions
          - native_type: returns native representation of its argument
          - TMGC(n): deals with GC(refcounting for given number of references (R)
          -
        Looks up Return type and then target-language independently creates lists of its content.
        """
        jparams = self.algocfg[ns + '::' + algo]['result_typemap']
        if len(jparams) > 0:
            jparams['ns'] = ns
            jparams['algo'] = algo
            t = jenv.from_string(typemap_wrapper_template)
            return (t.render(**jparams) + '\n').split('%SNIP%')
        return '', '', ''

    def lp(self, t):
        tmp = t.split('\n')
        for i in range(len(tmp)):
            print(i, tmp[i])

    ##################################################################################
    def gen_wrapper(self, ns, algo):
        """
        Here we actually generate the wrapper code. Separating this from preparation
        allows us to cross-reference between algos, for example for multi-phased algos.

        We combine the argument (template, input, parameter) information appropriately.
        We take care of the right order and bring them in the right format for our jinja templates.
        We pass them to the templates in a dict jparams, used a globals vars for jinja.

        Handling single-phased algos only which are not part of a multi-phased algo
        """
        cfg = self.algocfg[ns + '::' + algo]
        cpp_begin, pyx_begin, pyx_end, typesstr = '', '', '', ''

        cpp_map, cpp_end, pyx_map = self.gen_modelmaps(ns, algo)
        a, b, c = self.gen_resultmaps(ns, algo)
        cpp_map += a
        cpp_end += b
        pyx_map += c

        if len(cfg['params']) == 0:
            return (cpp_map, cpp_begin, cpp_end, pyx_map, pyx_begin, pyx_end, typesstr)


        jparams = cfg['params'].copy()
        jparams['create'] = cfg['create']
        jparams['add_setup']  = cfg['add_setup']
        jparams['model_maps'] = cfg['model_typemap']
        jparams['result_map'] = cfg['result_typemap']

        for p in ['distributed', 'streaming']:
            if p in cfg:
                jparams[p] = cfg[p]
        tdecl = cfg['sparams']

        t = jenv.from_string(algo_iface_template)
        cpp_begin += t.render(**jparams) + '\n'
        # render all specializations
        i = 0
        for td in tdecl:
            # Last but not least, we need to provide the template parameter specs
            jparams['template_decl'] = td['template_decl']
            jparams['template_args'] = td['template_args']
            jparams['incomplete'] = 'incomplete' in td
            jparams['params_req'] = td['params_req']
            jparams['params_opt'] = td['params_opt']
            jparams['params_get'] = td['params_get']
            jparams['params_ds'] = td['params_req'] + td['params_opt'] + [cfg['distributed'], cfg['streaming']]
            jparams['params_all'] = td['params_req'] + (td['template_args'] if td['template_args'] else []) + td['params_opt'] + [cfg['distributed'], cfg['streaming']]
            jparams['args_all']   = jparams['input_args'] + td['params_req'] + td['params_opt']
            # Very simple for specializations
            # but how do we pass only the required args to them from the wrapper?
            # we could have the full input list, but that doesn't work for required parameters
            assert td['template_args'] != None
            if 'dist' in cfg:
                # a wrapper for distributed mode
                assert len(tdecl) == 1
                jparams.update(cfg['dist'])
            t = jenv.from_string(manager_wrapper_template)
            cpp_begin += t.render(**jparams) + '\n'
            if td['pargs'] == None:
                t = jenv.from_string(hpat_spec_template)
                pyx_begin += t.render(**jparams) + '\n'
                # this is our actual API wrapper, only once per template (covering all its specializations)
                # the parent class
                t = jenv.from_string(parent_wrapper_template)
                pyx_end += t.render(**jparams) + '\n'
                # the C function generating specialized classes
                t = jenv.from_string(algo_wrapper_template)
                cpp_end += t.render(**jparams) + '\n'

            i = i+1

        return (cpp_map, cpp_begin, cpp_end, pyx_map, pyx_begin, pyx_end, typesstr)


    ##################################################################################
    def gen_footers(self, no_dist=False, no_stream=False):
        if no_dist:
            return ('', '', '')
        else:
            return ('', '', '#include "dist_logistic_regression.h"\n#include "dist_kmeans_init.h"\n#include "dist_kmeans.h"\n')
