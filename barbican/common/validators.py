#  Licensed under the Apache License, Version 2.0 (the "License"); you may
#  not use this file except in compliance with the License. You may obtain
#  a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#  WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#  License for the specific language governing permissions and limitations
#  under the License.
"""
API JSON validators.
"""

import abc

import jsonschema as schema
from oslo.config import cfg
import six

from barbican.common import exception
from barbican.common import utils
from barbican.model import models
from barbican.openstack.common import gettextutils as u
from barbican.openstack.common import timeutils
from barbican.plugin.util import mime_types


LOG = utils.getLogger(__name__)
DEFAULT_MAX_SECRET_BYTES = 10000
common_opts = [
    cfg.IntOpt('max_allowed_secret_in_bytes',
               default=DEFAULT_MAX_SECRET_BYTES),
]

CONF = cfg.CONF
CONF.register_opts(common_opts)


def secret_too_big(data):
    if isinstance(data, six.text_type):
        return len(data.encode('UTF-8')) > CONF.max_allowed_secret_in_bytes
    else:
        return len(data) > CONF.max_allowed_secret_in_bytes


def get_invalid_property(validation_error):
    # we are interested in the second item which is the failed propertyName.
    if validation_error.schema_path and len(validation_error.schema_path) > 1:
        return validation_error.schema_path[1]


@six.add_metaclass(abc.ABCMeta)
class ValidatorBase(object):
    """Base class for validators."""

    name = ''

    @abc.abstractmethod
    def validate(self, json_data, parent_schema=None):
        """Validate the input JSON.

        :param json_data: JSON to validate against this class' internal schema.
        :param parent_schema: Name of the parent schema to this schema.
        :returns: dict -- JSON content, post-validation and
        :                 normalization/defaulting.
        :raises: schema.ValidationError on schema violations.

        """

    def _full_name(self, parent_schema=None):
        """Validator schema name accessor

        Returns the full schema name for this validator,
        including parent name.
        """
        schema_name = self.name
        if parent_schema:
            schema_name = u._("{0}' within '{1}").format(self.name,
                                                         parent_schema)
        return schema_name

    def _assert_schema_is_valid(self, json_data, schema_name):
        """Assert that the JSON structure is valid for the given schema.

        :raises: InvalidObject exception if the data is not schema compliant.
        """
        try:
            schema.validate(json_data, self.schema)
        except schema.ValidationError as e:
            raise exception.InvalidObject(schema=schema_name,
                                          reason=e.message,
                                          property=get_invalid_property(e))

    def _assert_validity(self, valid_condition, schema_name, message,
                         property):
        """Assert that a certain condition is met.

        :raises: InvalidObject exception if the condition is not met.
        """
        if not valid_condition:
            raise exception.InvalidObject(schema=schema_name, reason=message,
                                          property=property)


class NewSecretValidator(ValidatorBase):
    """Validate a new secret."""

    def __init__(self):
        self.name = 'Secret'

        # TODO(jfwood): Get the list of mime_types from the crypto plugins?
        self.schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "algorithm": {"type": "string"},
                "mode": {"type": "string"},
                "bit_length": {"type": "integer", "minimum": 1},
                "expiration": {"type": "string"},
                "payload": {"type": "string"},
                "payload_content_type": {"type": "string"},
                "payload_content_encoding": {
                    "type": "string",
                    "enum": [
                        "base64"
                    ]
                },
                "transport_key_needed": {
                    "type": "string",
                    "enum": ["true", "false"]
                },
                "transport_key_id": {"type": "string"},
            },
        }

    def validate(self, json_data, parent_schema=None):
        """Validate the input JSON for the schema for secrets."""
        schema_name = self._full_name(parent_schema)
        self._assert_schema_is_valid(json_data, schema_name)

        json_data['name'] = self._extract_name(json_data)

        expiration = self._extract_expiration(json_data, schema_name)
        self._assert_expiration_is_valid(expiration, schema_name)
        json_data['expiration'] = expiration

        if 'payload' in json_data:
            content_type = json_data.get('payload_content_type')
            content_encoding = json_data.get('payload_content_encoding')
            self._validate_content_parameters(content_type, content_encoding,
                                              schema_name)

            payload = self._extract_payload(json_data)
            self._assert_validity(payload, schema_name,
                                  u._("If 'payload' specified, must be non "
                                      "empty"),
                                  "payload")
            json_data['payload'] = payload
        elif 'payload_content_type' in json_data:
            self._assert_validity(parent_schema is not None, schema_name,
                                  u._("payload must be provided when "
                                      "payload_content_type is specified"),
                                  "payload")

        return json_data

    def _extract_name(self, json_data):
        """Extracts and returns the name from the JSON data."""
        name = json_data.get('name')
        if name:
            return name.strip()
        return None

    def _extract_expiration(self, json_data, schema_name):
        """Extracts and returns the expiration date from the JSON data."""
        expiration = None
        expiration_raw = json_data.get('expiration')
        if expiration_raw and expiration_raw.strip():
            try:
                expiration_tz = timeutils.parse_isotime(expiration_raw.strip())
                expiration = timeutils.normalize_time(expiration_tz)
            except ValueError:
                LOG.exception("Problem parsing expiration date")
                raise exception.InvalidObject(
                    schema=schema_name,
                    reason=u._("Invalid date for 'expiration'"),
                    property="expiration")

        return expiration

    def _assert_expiration_is_valid(self, expiration, schema_name):
        """Asserts that the given expiration date is valid.

        Expiration dates must be in the future, not the past.
        """
        if expiration:
            # Verify not already expired.
            utcnow = timeutils.utcnow()
            self._assert_validity(expiration > utcnow, schema_name,
                                  u._("'expiration' is before current time"),
                                  "expiration")

    def _validate_content_parameters(self, content_type, content_encoding,
                                     schema_name):
        """Content parameter validator.

        Check that the content_type, content_encoding and the parameters
        that they affect are valid.
        """
        self._assert_validity(
            content_type is not None,
            schema_name,
            u._("If 'payload' is supplied, 'payload_content_type' must also "
                "be supplied."),
            "payload_content_type")

        self._assert_validity(
            content_type.lower() in mime_types.SUPPORTED,
            schema_name,
            u._("payload_content_type is not one of {0}").format(
                mime_types.SUPPORTED),
            "payload_content_type")

        if content_type == 'application/octet-stream':
            self._assert_validity(
                content_encoding is not None,
                schema_name,
                u._("payload_content_encoding must be specified when "
                    "payload_content_type is application/octet-stream."),
                "payload_content_encoding")

        if content_type.startswith('text/plain'):
            self._assert_validity(
                content_encoding is None,
                schema_name,
                u._("payload_content_encoding must not be specified when "
                    "payload_content_type is text/plain"),
                "payload_content_encoding")

    def _extract_payload(self, json_data):
        """Extracts and returns the payload from the JSON data.

        :raises: LimitExceeded if the payload is too big
        """
        payload = json_data.get('payload', '')
        if secret_too_big(payload):
            raise exception.LimitExceeded()

        return payload.strip()


# TODO(atiwari) - Split this validator module and unit tests
# into smaller modules
class TypeOrderValidator(ValidatorBase):
    """Validate a new typed order."""

    def __init__(self):
        self.name = 'Order'
        self.schema = {
            "type": "object",
            "$schema": "http://json-schema.org/draft-03/schema",
            "properties": {"meta": {"type": "object"},
                           "type": {"type": "string",
                                    "required": True,
                                    "enum": ['key', 'asymmetric',
                                             'certificate']}
                           }}

    def validate(self, json_data, parent_schema=None):
        schema_name = self._full_name(parent_schema)

        self._assert_schema_is_valid(json_data, schema_name)

        order_type = json_data.get('type').lower()
        # Note(atiwari): No support for certificate so far
        if order_type == models.OrderType.CERTIFICATE:
            certificate_meta = json_data.get('meta')
            self._validate_certificate_meta(certificate_meta, schema_name)

        elif order_type == models.OrderType.ASYMMETRIC:
            asymmetric_meta = json_data.get('meta')
            self._validate_asymmetric_meta(asymmetric_meta, schema_name)

        elif order_type == models.OrderType.KEY:
            key_meta = json_data.get('meta')
            self._validate_key_meta(key_meta, schema_name)
        else:
            self._raise_feature_not_implemented(order_type, schema_name)

        return json_data

    def _validate_key_meta(self, key_meta, schema_name):
        """Validation specific to meta for key type order."""

        self._assert_validity(key_meta is not None,
                              schema_name,
                              u._("'meta' attributes is required"), "meta")
        secret_validator = NewSecretValidator()
        secret_validator.validate(key_meta, parent_schema=self.name)

        self._assert_validity(key_meta.get('payload') is None,
                              schema_name,
                              u._("'payload' not allowed "
                                  "for key type order"), "meta")

        # Validation secret generation related fields.
        # TODO(jfwood): Invoke the crypto plugin for this purpose

        self._validate_bit_length(key_meta, schema_name)

    def _validate_asymmetric_meta(self, asymmetric_meta, schema_name):
        """Validation specific to meta for asymmetric type order."""
        self._assert_validity(asymmetric_meta is not None,
                              schema_name,
                              u._("'meta' attributes is required"), "meta")

    def _validate_certificate_meta(self, certificate_meta, schema_name):
        """Validation specific to meta for certificate type order."""
        self._assert_validity(certificate_meta is not None,
                              schema_name,
                              u._("'meta' attributes is required"), "meta")

    def _extract_expiration(self, json_data, schema_name):
        """Extracts and returns the expiration date from the JSON data."""
        expiration = None
        expiration_raw = json_data.get('expiration', None)
        if expiration_raw and expiration_raw.strip():
            try:
                expiration_tz = timeutils.parse_isotime(expiration_raw)
                expiration = timeutils.normalize_time(expiration_tz)
            except ValueError:
                LOG.exception("Problem parsing expiration date")
                raise exception.InvalidObject(schema=schema_name,
                                              reason=u._("Invalid date "
                                                         "for 'expiration'"),
                                              property="expiration")

        return expiration

    def _validate_bit_length(self, key_meta, schema_name):

        bit_length = int(key_meta.get('bit_length', 0))
        if bit_length <= 0:
            raise exception.UnsupportedField(field="bit_length",
                                             schema=schema_name,
                                             reason=u._("Must have "
                                                        "non-zero positive"
                                                        " bit_length to"
                                                        " generate secret"
                                                        ))
        if bit_length % 8 != 0:
            raise exception.UnsupportedField(field="bit_length",
                                             schema=schema_name,
                                             reason=u._("Must be a"
                                                        " positive integer"
                                                        " that is a"
                                                        " multiple of 8"))

    def _raise_feature_not_implemented(self, order_type, schema_name):
        raise exception.FeatureNotImplemented(field='type',
                                              schema=schema_name,
                                              reason=u._("Feature not "
                                                         "implemented for "
                                                         "'{0}' order type")
                                                    .format(order_type))


# TODO(atiwari) - Remove this Validator for bug 1335171
class NewOrderValidator(ValidatorBase):
    """Validate a new order."""

    def __init__(self):
        self.name = 'Order'
        self.schema = {
            "type": "object",
            "properties": {
            },
        }
        self.secret_validator = NewSecretValidator()

    def validate(self, json_data, parent_schema=None):
        schema_name = self._full_name(parent_schema)

        self._assert_schema_is_valid(json_data, schema_name)

        secret = json_data.get('secret')
        self._assert_validity(secret is not None,
                              schema_name,
                              u._("'secret' attributes are required"),
                              "secret")

        # If secret group is provided, validate it now.
        self.secret_validator.validate(secret, parent_schema=self.name)
        self._assert_validity('payload' not in secret,
                              schema_name,
                              u._("'payload' not allowed for secret "
                                  "generation"),
                              "secret")

        # Validation secret generation related fields.
        # TODO(jfwood): Invoke the crypto plugin for this purpose

        if secret.get('payload_content_type') != 'application/octet-stream':
            raise exception.UnsupportedField(field='payload_content_type',
                                             schema=schema_name,
                                             reason=u._("Only 'application/oc "
                                                        "tet-stream' "
                                                        "supported"))

        return json_data


class ContainerConsumerValidator(ValidatorBase):
    """Validate a Consumer."""

    def __init__(self):
        self.name = 'Consumer'
        self.schema = {
            "type": "object",
            "properties": {
                "URL": {"type": "string"},
                "name": {"type": "string"}
            },
            "required": ["name", "URL"]
        }

    def validate(self, json_data, parent_schema=None):
        schema_name = self._full_name(parent_schema)

        self._assert_schema_is_valid(json_data, schema_name)
        return json_data


class ContainerValidator(ValidatorBase):
    """Validator for all types of Container."""

    def __init__(self):
        self.name = 'Container'
        self.schema = {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "type": {
                    "type": "string",
                    # TODO(hgedikli): move this to a common location
                    "enum": ["generic", "rsa", "certificate"]
                },
                "secret_refs": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["secret_ref"],
                        "properties": {
                            "secret_ref": {"type": "string", "minLength": 1}
                        }
                    }
                }
            },
            "required": ["type"]
        }

    def validate(self, json_data, parent_schema=None):
        schema_name = self._full_name(parent_schema)

        self._assert_schema_is_valid(json_data, schema_name)

        container_type = json_data.get('type')
        secret_refs = json_data.get('secret_refs')

        if not secret_refs:
            return json_data

        secret_refs_names = set(secret_ref.get('name', '')
                                for secret_ref in secret_refs)

        self._assert_validity(
            len(secret_refs_names) == len(secret_refs),
            schema_name,
            u._("Duplicate reference names are not allowed"),
            "secret_refs")

        # The combination of container_id and secret_id is expected to be
        # primary key for container_secret so same secret id (ref) cannot be
        # used within a container
        secret_ids = set(self._get_secret_id_from_ref(secret_ref)
                         for secret_ref in secret_refs)

        self._assert_validity(
            len(secret_ids) == len(secret_refs),
            schema_name,
            u._("Duplicate secret ids are not allowed"),
            "secret_refs")

        if container_type == 'rsa':
            self._validate_rsa(secret_refs_names, schema_name)
        elif container_type == 'certificate':
            self._validate_certificate(secret_refs_names, schema_name)

        return json_data

    def _validate_rsa(self, secret_refs_names, schema_name):
        required_names = set(['public_key', 'private_key'])
        optional_names = set(['private_key_passphrase'])
        contains_unsupported_names = self._contains_unsupported_names(
            secret_refs_names, required_names | optional_names)
        self._assert_validity(
            not contains_unsupported_names,
            schema_name,
            u._("only 'private_key', 'public_key' and "
                "'private_key_passphrase' reference names are "
                "allowed for RSA type"),
            "secret_refs")

        self._assert_validity(
            self._has_minimum_required(secret_refs_names, required_names),
            schema_name,
            u._("The minimum required reference names are 'public_key' and"
                "'private_key' for RSA type"),
            "secret_refs")

    def _validate_certificate(self, secret_refs_names, schema_name):
        required_names = set(['certificate'])
        optional_names = set(['private_key', 'private_key_passphrase',
                              'intermediates'])
        contains_unsupported_names = self._contains_unsupported_names(
            secret_refs_names, required_names.union(optional_names))
        self._assert_validity(
            not contains_unsupported_names,
            schema_name,
            u._("only 'private_key', 'certificate' , "
                "'private_key_passphrase',  or 'intermediates' "
                "reference names are allowed for Certificate type"),
            "secret_refs")

        self._assert_validity(
            self._has_minimum_required(secret_refs_names, required_names),
            schema_name,
            u._("The minimum required reference name is 'certificate' "
                "for Certificate type"),
            "secret_refs")

    def _contains_unsupported_names(self, secret_refs_names, supported_names):
        if secret_refs_names.difference(supported_names):
            return True
        return False

    def _has_minimum_required(self, secret_refs_names, required_names):
        if required_names.issubset(secret_refs_names):
            return True
        return False

    def _get_secret_id_from_ref(self, secret_ref):
        secret_id = secret_ref.get('secret_ref')
        if secret_id.endswith('/'):
            secret_id = secret_id.rsplit('/', 2)[1]
        elif '/' in secret_id:
            secret_id = secret_id.rsplit('/', 1)[1]

        return secret_id


class NewTransportKeyValidator(ValidatorBase):
    """Validate a new transport key."""

    def __init__(self):
        self.name = 'Transport Key'

        self.schema = {
            "type": "object",
            "properties": {
                "plugin_name": {"type": "string"},
                "transport_key": {"type": "string"},
            },
        }

    def validate(self, json_data, parent_schema=None):
        schema_name = self._full_name(parent_schema)

        self._assert_schema_is_valid(json_data, schema_name)

        plugin_name = json_data.get('plugin_name', '').strip()
        self._assert_validity(plugin_name,
                              schema_name,
                              u._("plugin_name must be provided"),
                              "plugin_name")
        json_data['plugin_name'] = plugin_name

        transport_key = json_data.get('transport_key', '').strip()
        self._assert_validity(transport_key,
                              schema_name,
                              u._("transport_key must be provided"),
                              "transport_key")
        json_data['transport_key'] = transport_key

        return json_data
