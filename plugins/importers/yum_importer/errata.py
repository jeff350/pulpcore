# -*- coding: utf-8 -*-
#
# Copyright © 2012 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public
# License as published by the Free Software Foundation; either version
# 2 of the License (GPLv2) or (at your option) any later version.
# There is NO WARRANTY for this software, express or implied,
# including the implied warranties of MERCHANTABILITY,
# NON-INFRINGEMENT, or FITNESS FOR A PARTICULAR PURPOSE. You should
# have received a copy of GPLv2 along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.

"""
Errata Support for Yum Importer
"""
import os
import sys
import time
import yum
import logging
import updateinfo
from util import get_repomd_filetype_path, get_repomd_filetypes
from pulp.server.managers.repo.unit_association_query import Criteria

_LOG = logging.getLogger(__name__)
#TODO Fix up logging so we log to a separate file to aid debugging
#_LOG.addHandler(logging.FileHandler('/var/log/pulp/yum-importer.log'))

ERRATA_TYPE_ID="erratum"
ERRATA_UNIT_KEY = ("id",)

ERRATA_METADATA = ("title", "description", "version", "release", "type", "status", "updated",
                "issued", "severity", "references", "pkglist", "rights",  "summary",
                "solution", "from_str", "pushcount" )

def get_available_errata(repo_dir):
    """
        Check and Parses the updateinfo.xml and extract errata items to sync

        @param repo_dir repo directory where the metadata can be found
        @type repo_dir str

        @return a dict of errata items with errata id as key
        @rtype {'':{}}
    """
    repomd_xml = os.path.join(repo_dir, "repodata/repomd.xml")
    ftypes = get_repomd_filetypes(repomd_xml)
    errata_from_xml = {}
    if "updateinfo" not in ftypes:
        return errata_from_xml
    updateinfo_xml_path = os.path.join(repo_dir, get_repomd_filetype_path(repomd_xml, "updateinfo"))
    if not os.path.exists(updateinfo_xml_path):
        return {}
    try:
        errata_from_xml = updateinfo.get_errata(updateinfo_xml_path)
    except yum.Errors.YumBaseError, e:
        _LOG.error("Error parsing updateinfo file [%s]; Error: %s" % (updateinfo_xml_path, e))
    errata_items = {}
    for e_obj in errata_from_xml:
        errata_items[e_obj['id']] = e_obj
    return errata_items

def get_existing_errata(sync_conduit, criteria=None):
    """
     Lookup existing erratum type units in pulp

     @param sync_conduit
     @type sync_conduit pulp.server.content.conduits.repo_sync.RepoSyncConduit

     @param criteria
     @type criteria pulp.server.managers.repo.unit_association_query.Criteria

     @return a dictionary of existing units, key is the errata id and the value is the unit
     @rtype {():pulp.server.content.plugins.model.Unit}
    """
    existing_units = {}
    for u in sync_conduit.get_units(criteria):
        key = u.unit_key['id']
        existing_units[key] = u
    return existing_units

def get_orphaned_errata(available_errata, existing_errata):
    """
    @param available_errata a dict of errata
    @type available_errata {}

    @param existing_errata dict of units
    @type existing_errata {key:pulp.server.content.plugins.model.Unit}

    @return a dictionary of orphaned units, key is the errata id and the value is the unit
    @rtype {key:pulp.server.content.plugins.model.Unit}
    """
    orphaned_errata = {}
    for key in existing_errata:
        if key not in available_errata:
            orphaned_errata[key] = existing_errata[key]
    return orphaned_errata

def get_new_errata_units(available_errata, existing_errata, sync_conduit):
    """
        Determines which errata to add  or remove and will initialize new units

        @param available_errata a dict of available errata
        @type available_errata {}

        @param existing_errata dict of existing errata Units
        @type existing_errata {pulp.server.content.plugins.model.Unit}

        @param sync_conduit
        @type sync_conduit pulp.server.content.conduits.repo_sync.RepoSyncConduit

        @return a tuple of 2 dictionaries.  First dict is of new errata, second dict is of new units
        @rtype ({}, {}, pulp.server.content.conduits.repo_sync.RepoSyncConduit)
    """
    new_errata = {}
    new_units = {}
    for key in available_errata:
        if key in existing_errata:
            existing_erratum = existing_errata[key]
            if available_errata[key]['updated'] <= existing_erratum.updated:
                _LOG.info("Errata [%s] already exists and latest; skipping" % existing_erratum)
                continue
            # remove if erratum already exist so we can update it
            _LOG.info("Removing Errata unit %s to update " % existing_erratum)
            sync_conduit.remove_unit(existing_erratum)
        erratum = available_errata[key]
        new_errata[key] = erratum
        unit_key  = form_errata_unit_key(erratum)
        metadata =  form_errata_metadata(erratum)
        new_units[key] = sync_conduit.init_unit(ERRATA_TYPE_ID, unit_key, metadata, None)
    return new_errata, new_units, sync_conduit

def _sync(repo, sync_conduit,  config):
    """
      Invokes errata sync sequence

      @param repo: metadata describing the repository
      @type  repo: L{pulp.server.content.plugins.data.Repository}

      @param sync_conduit
      @type sync_conduit pulp.server.content.conduits.repo_sync.RepoSyncConduit

      @param config: plugin configuration
      @type  config: L{pulp.server.content.plugins.config.PluginCallConfiguration}

      @return a tuple of state, dict of sync summary and dict of sync details
      @rtype (bool, {}, {})
    """
    start = time.time()
    repo_dir = "%s/%s" % (repo.working_dir, repo.id)
    available_errata = get_available_errata(repo_dir)
    _LOG.info("Available Errata %s" % len(available_errata))
    progress = ErrataProgress().__dict__
    sync_conduit.set_progress(progress)
    criteria = Criteria(type_ids=ERRATA_TYPE_ID)
    existing_errata = get_existing_errata(sync_conduit, criteria=criteria)
    _LOG.info("Existing Errata %s" % len(existing_errata))
    orphaned_units = get_orphaned_errata(available_errata, existing_errata)
    new_errata, new_units, sync_conduit = get_new_errata_units(available_errata, existing_errata, sync_conduit)
    # Save the new units
    for u in new_units.values():
        sync_conduit.save_unit(u)

    # clean up any orphaned errata
    for u in orphaned_units.values():
        sync_conduit.remove_unit(u)
    # get errata sync details
    errata_details = errata_sync_details(new_errata)
    end = time.time()

    summary = dict()
    summary["num_new_errata"] = len(new_errata)
    summary["num_existing_errata"] = len(existing_errata)
    summary["num_orphaned_errata"] = len(orphaned_units)
    summary["errata_time_total_sec"] = end - start

    details = dict()
    details["num_bugfix_errata"] = len(errata_details['types']['bugfix'])
    details["num_security_errata"] = len(errata_details['types']['security'])
    details["num_enhancement_errata"] = len(errata_details['types']['enhancement'])
    _LOG.info("Errata Summary: %s \n Details: %s" % (summary, details))
    return True, summary, details

def form_errata_unit_key(erratum):
    unit_key = {}
    for key in ERRATA_UNIT_KEY:
        unit_key[key] = erratum[key]
    return unit_key

def form_errata_metadata(erratum):
    metadata = {}
    for key in ERRATA_METADATA:
        metadata[key] = erratum[key]
    return metadata

def errata_sync_details(errata_list):
    errata_details = dict()
    errata_details['types'] = {'bugfix' : [], 'security' : [], 'enhancement' : [], }
    for erratum in errata_list.values():
        if erratum['type'] == 'bugfix':
            errata_details['types']["bugfix"].append(erratum)
        elif erratum['type'] == 'security':
            errata_details['types']["security"].append(erratum)
        elif erratum['type'] == 'enhancement':
            errata_details['types']["enhancement"].append(erratum)
    return errata_details

class ErrataProgress(object):
    def __init__(self, step="Importing Errata"):
        self.step = step
        self.details = {}
