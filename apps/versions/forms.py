import collections
import hashlib
import os
import shutil
import time
import zipfile
from xml.dom.minidom import parse

from django import forms
from django.conf import settings

import commonware.log
from tower import ugettext as _
import happyforms

import amo
from addons.models import Addon, AddonUser
from files.models import File
from versions.models import ApplicationsVersions, AppVersion, License, Version

license_ids = dict((license.shortname, license.id) for license in amo.LICENSES)

log = commonware.log.getLogger('z.addons')


class LicenseForm(happyforms.Form):
    license_type = forms.ChoiceField(choices=license_ids.iteritems(),
                                     required=False)
    license_text = forms.CharField(required=False)

    def clean_license_type(self):
        type = self.cleaned_data['license_type']

        if not type:
            return 'other'

        if type in license_ids:
            return type

    def clean(self):
        # Raise error if we get text and something other than other
        # or if we get no text with other.
        type = self.cleaned_data.get('license_type', 'other')
        text = self.cleaned_data['license_text']

        if type == 'other' and not text:
            raise forms.ValidationError(_('License text missing.'))

        if text and type != 'other':
            raise forms.ValidationError(
                    _('Select "other" if supplying a custom license.'))

        return self.cleaned_data

    def get_or_create(self):
        """Gives us an existing license.id or a new id for a license."""
        data = self.cleaned_data
        if data['license_type'] == 'other':
            return License.objects.create(text=data['license_text'])
        else:
            builtin = license_ids[data['license_type']]
            return License.objects.get(builtin=builtin)


def get_text_value(xml, tag):
    node = xml.getElementsByTagName('em:%s' % tag)[0]
    if node.childNodes:
        textnode = node.childNodes[0]
        return textnode.wholeText


def parse_xpi(xpi, addon=None):
    # Extract to /tmp
    path = os.path.join(settings.TMP_PATH, str(time.time()))
    os.makedirs(path)

    # Validating that we have no member files that try to break out of
    # the destination path.  NOTE: This will be obsolete when this bug is
    # fixed: http://bugs.python.org/issue6972
    zip = zipfile.ZipFile(xpi)

    for f in zip.namelist():
        if '..' in f or f.startswith('/'):
            raise forms.ValidationError(_('Invalid archive.'))

    zip.extractall(path)

    # read RDF and store in clean_data
    rdf = parse(os.path.join(path, 'install.rdf'))
    # XPIs use their own type ids
    XPI_TYPES = {'2': amo.ADDON_EXTENSION, '4': amo.ADDON_THEME,
                 '8': amo.ADDON_LPADDON}

    apps = []
    App = collections.namedtuple('App', 'appdata id min max')
    for node in rdf.getElementsByTagName('em:targetApplication'):
        app = amo.APP_GUIDS.get(get_text_value(node, 'id'))
        min_val = get_text_value(node, 'minVersion')
        max_val = get_text_value(node, 'maxVersion')

        try:
            min = AppVersion.objects.get(application=app.id,
                                         version=min_val)
            max = AppVersion.objects.get(application=app.id,
                                         version=max_val)
        except AppVersion.DoesNotExist:
            continue

        if app:
            apps.append(App(appdata=app, id=app.id, min=min, max=max))

    guid = get_text_value(rdf, 'id')

    if addon and addon.guid != guid:
        raise forms.ValidationError(_("GUID doesn't match add-on"))
    if not addon and Addon.objects.filter(guid=guid):
        raise forms.ValidationError(_('Duplicate GUID found.'))

    shutil.rmtree(path)

    return dict(
                guid=guid,
                name=get_text_value(rdf, 'name'),
                description=get_text_value(rdf, 'description'),
                version=get_text_value(rdf, 'version'),
                homepage=get_text_value(rdf, 'homepageURL'),
                type=XPI_TYPES.get(get_text_value(rdf, 'type')),
                apps=apps,
           )


class XPIForm(happyforms.Form):
    """
    Validates a new XPI.
    * Checks for duplicate GUID
    """

    platform = forms.ChoiceField(
                choices=[(p.shortname, p.name) for p in amo.PLATFORMS.values()
                         if p != amo.PLATFORM_ANY], required=False,)

    xpi = forms.FileField(required=True)

    def __init__(self, data, files, addon=None):
        self.addon = addon
        super(XPIForm, self).__init__(data, files)

    def clean_platform(self):
        return self.cleaned_data['platform'] or amo.PLATFORM_ALL.shortname

    def clean_xpi(self):
        # TODO(basta): connect to addon validator.
        xpi = self.cleaned_data['xpi']
        self.cleaned_data.update(parse_xpi(xpi, self.addon))
        return xpi

    def create_addon(self, user, license=None):
        data = self.cleaned_data
        a = Addon(guid=data['guid'],
                  name=data['name'],
                  type=data['type'],
                  status=amo.STATUS_UNREVIEWED,
                  homepage=data['homepage'],
                  description=data['description'])
        a.save()
        AddonUser(addon=a, user=user).save()

        self.addon = a
        # Save Version, attach License
        self.create_version(license=license)
        log.info('Addon %d saved' % a.id)
        return a

    def _save_file(self, version):
        data = self.cleaned_data
        xpi = data['xpi']
        hash = hashlib.sha256()
        path = os.path.join(settings.ADDONS_PATH, str(version.addon.id))
        if not os.path.exists(path):
            os.mkdir(path)

        f = File(version=version,
                 platform_id=amo.PLATFORM_DICT[data['platform']].id,
                 size=xpi.size)

        filename = f.generate_filename()

        with open(os.path.join(path, filename), 'w') as destination:
            for chunk in xpi.chunks():
                hash.update(chunk)
                destination.write(chunk)

        f.hash = 'sha256:%s' % hash.hexdigest()
        f.save()
        return f

    def create_version(self, license=None):
        data = self.cleaned_data
        v = Version(addon=self.addon, license=license,
                    version=data['version'])
        v.save()
        apps = data['apps']
        for app in apps:
            av = ApplicationsVersions(version=v, min=app.min, max=app.max,
                                      application_id=app.id)
            av.save()

        self._save_file(v)
        return v
