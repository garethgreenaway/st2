# Licensed to the StackStorm, Inc ('StackStorm') under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import abc
import copy
import functools
import inspect

import jsonschema
import six
from six.moves import http_client
from webob import exc
import pecan
import pecan.jsonify

from st2common.util import mongoescape as util_mongodb
from st2common.util import schema as util_schema
from st2common.util.jsonify import json_encode
from st2common import log as logging
from st2common.constants.auth import QUERY_PARAM_ATTRIBUTE_NAME


LOG = logging.getLogger(__name__)
VALIDATOR = util_schema.get_validator(assign_property_default=False)

# A list of method names for which we don't want to log the result / response
RESPONSE_LOGGING_METHOD_NAME_BLACKLIST = [
    'get_all'
]

# A list of controller classes for which we don't want to log the result / response
RESPONSE_LOGGING_CONTROLLER_NAME_BLACKLIST = [
    'ActionExecutionChildrenController',  # action executions can be big
    'ActionExecutionAttributeController',  # result can be big
    'ActionExecutionsController'  # action executions can be big
]


@six.add_metaclass(abc.ABCMeta)
class BaseAPI(object):
    schema = abc.abstractproperty

    def __init__(self, **kw):
        VALIDATOR(getattr(self, 'schema', {})).validate(kw)

        for key, value in kw.items():
            setattr(self, key, value)

    def __repr__(self):
        name = type(self).__name__
        attrs = ', '.join("'%s':%r" % item for item in six.iteritems(vars(self)))
        # The format here is so that eval can be applied.
        return "%s(**{%s})" % (name, attrs)

    def __str__(self):
        name = type(self).__name__
        attrs = ', '.join("%s=%r" % item for item in six.iteritems(vars(self)))

        return "%s[%s]" % (name, attrs)

    def __json__(self):
        return vars(self)

    @classmethod
    def _from_model(cls, model):
        doc = util_mongodb.unescape_chars(model.to_mongo())
        if '_id' in doc:
            doc['id'] = str(doc.pop('_id'))
        return doc

    @classmethod
    def from_model(cls, model):
        doc = cls._from_model(model)
        attrs = {attr: value for attr, value in six.iteritems(doc) if value is not None}
        return cls(**attrs)

    @classmethod
    def to_model(cls, doc):
        model = cls.model()
        setattr(model, 'name', getattr(doc, 'name', None))
        setattr(model, 'description', getattr(doc, 'description', None))
        return model


def _handle_error(e, status_code, body=None, headers=None):
    """
    Encodes error into a json response and returns. This was the default
    response to an error, HTML page, is skipped.
    """
    pecan.response.status = status_code
    if headers:
        pecan.response.headers = headers
    # re-purposing body this way has drawbacks but the other options i.e.
    # to envelope the body inside error_body would be awkward for clients.
    error_body = body if body else dict()
    assert isinstance(error_body, dict)
    error_body['faultstring'] = e.message
    return json_encode(error_body)


def jsexpose(arg_types=None, body_cls=None, status_code=None, content_type='application/json'):
    """
    :param arg_types: A list of types for the function arguments.
    :type arg_types: ``list``

    :param body_cls: Request body class. If provided, this class will be used to create an instance
                     out of the request body.
    :type body_cls: :class:`object`

    :param status_code: Response status code.
    :type status_code: ``int``

    :param content_type: Response content type.
    :type content_type: ``str``
    """
    pecan_json_decorate = pecan.expose(
        content_type=content_type,
        generic=False)

    def decorate(f):
        @functools.wraps(f)
        def callfunction(*args, **kwargs):
            controller = args[0] if args else None

            # Note: We use getattr since in some places (tests) request is mocked
            params = getattr(pecan.request, 'params', {})
            method = getattr(pecan.request, 'method', None)
            path = getattr(pecan.request, 'path', None)
            remote_addr = getattr(pecan.request, 'remote_addr', None)

            # Common request information included in the log context
            request_info = {'method': method, 'path': path, 'remote_addr': remote_addr}

            # Log the incoming request
            values = copy.copy(request_info)
            values['filters'] = kwargs
            LOG.info('%(method)s %(path)s with filters=%(filters)s' % values, extra=values)

            if QUERY_PARAM_ATTRIBUTE_NAME in params and QUERY_PARAM_ATTRIBUTE_NAME in kwargs:
                # Remove auth token if one is provided via query params
                del kwargs[QUERY_PARAM_ATTRIBUTE_NAME]

            try:
                args = list(args)
                types = copy.copy(arg_types)
                more = [args.pop(0)]

                if types:
                    argspec = inspect.getargspec(f)
                    names = argspec.args[1:]

                    for name in names:
                        try:
                            a = args.pop(0)
                            more.append(types.pop(0)(a))
                        except IndexError:
                            try:
                                kwargs[name] = types.pop(0)(kwargs[name])
                            except IndexError:
                                LOG.warning("Type definition for '%s' argument of '%s' "
                                            "is missing.", name, f.__name__)
                            except KeyError:
                                pass

                if body_cls:
                    if pecan.request.body:
                        data = pecan.request.json
                    else:
                        data = {}
                    try:
                        obj = body_cls(**data)
                    except jsonschema.exceptions.ValidationError as e:
                        return _handle_error(e, http_client.BAD_REQUEST)
                    more.append(obj)

                args = tuple(more) + tuple(args)

                noop_codes = [http_client.NOT_IMPLEMENTED,
                              http_client.METHOD_NOT_ALLOWED,
                              http_client.FORBIDDEN]

                if status_code and status_code in noop_codes:
                    pecan.response.status = status_code
                    return json_encode(None)

                try:
                    result = f(*args, **kwargs)

                    # Log the outgoing response
                    values = copy.copy(request_info)
                    values['status_code'] = status_code or pecan.response.status

                    function_name = f.__name__
                    controller_name = controller.__class__.__name__

                    log_result = True
                    log_result &= function_name not in RESPONSE_LOGGING_METHOD_NAME_BLACKLIST
                    log_result &= controller_name not in RESPONSE_LOGGING_CONTROLLER_NAME_BLACKLIST

                    if log_result:
                        values['result'] = result
                        log_msg = '%(method)s %(path)s result=%(result)s' % values
                    else:
                        # Note: We don't want to include a result for some
                        # methods which have a large result
                        log_msg = '%(method)s %(path)s' % values

                    LOG.info(log_msg, extra=values)

                    if status_code:
                        pecan.response.status = status_code
                    if content_type == 'application/json':
                        return json_encode(result)
                    else:
                        return result
                except exc.HTTPUnauthorized as e:
                    LOG.debug('API call failed: %s' % (str(e)))
                    return _handle_error(e, e.wsgi_response.status_code, e.wsgi_response.body,
                                         e.headers)
                except exc.HTTPException as e:
                    LOG.exception('API call failed: %s' % (str(e)))
                    # Exception contains pecan.response.header + more. This is per implementation
                    # of the WSGIHTTPException type from WebOb.
                    return _handle_error(e, e.wsgi_response.status_code, e.wsgi_response.body,
                                         e.headers)

            except Exception as e:
                LOG.exception('API call failed: %s' % (str(e)))
                return _handle_error(e, http_client.INTERNAL_SERVER_ERROR)

        pecan_json_decorate(callfunction)

        return callfunction

    return decorate
