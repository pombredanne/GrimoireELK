#!/usr/bin/python3
# -*- coding: utf-8 -*-
#
# Gerrit to Elastic class helper
#
# Copyright (C) 2015 Bitergia
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place - Suite 330, Boston, MA 02111-1307, USA.
#
# Authors:
#   Alvaro del Castillo San Felix <acs@bitergia.com>
#

from datetime import datetime
from dateutil import parser
import json
import logging
import requests
import time


from grimoire.elk.enrich import Enrich

class GerritEnrich(Enrich):

    def __init__(self, gerrit, sortinghat=True, db_projects_map = None):
        super().__init__(sortinghat, db_projects_map)
        self.elastic = None
        self.gerrit = gerrit
        self.type_name = "items"  # type inside the index to store items enriched

    def set_elastic(self, elastic):
        self.elastic = elastic

    def get_field_date(self):
        return "metadata__updated_on"

    def get_fields_uuid(self):
        return ["review_uuid", "patchSet_uuid", "approval_uuid"]

    def get_field_unique_id(self):
        return "ocean-unique-id"

    @classmethod
    def get_sh_identity(cls, user):
        identity = {}
        for field in ['name', 'email', 'username']:
            identity[field] = None
        if 'name' in user: identity['name'] = user['name']
        if 'email' in user: identity['email'] = user['email']
        if 'username' in user: identity['username'] = user['username']
        return identity

    def get_item_sh(self, item):
        """ Add sorting hat enrichment fields """
        eitem = {}  # Item enriched

        item = item['data']

        identity = GerritEnrich.get_sh_identity(item['owner'])
        eitem["uuid"] = self.get_uuid(identity, self.get_connector_name())
        eitem["name"] = identity['name']

        enrollments = self.get_enrollments(eitem["uuid"])
        # TODO: get the org_name for the current commit time
        if len(enrollments) > 0:
            eitem["org_name"] = enrollments[0].organization.name
        else:
            eitem["org_name"] = None
        # bot
        u = self.get_unique_identities(eitem["uuid"])[0]
        if u.profile:
            eitem["bot"] = u.profile.is_bot
        else:
            eitem["bot"] = False  # By default, identities are not bots
        eitem["bot"] = 0  # Not supported yet

        if identity['email']:
            try:
                eitem["domain"] = identity['email'].split("@")[1]
            except IndexError:
                logging.warning("Bad email format: %s" % (identity['email']))
                eitem["domain"] = None
        else:
            eitem["domain"] = None

        # Unify fields name
        eitem["author_uuid"] = eitem["uuid"]
        eitem["author_name"] = eitem["name"]
        eitem["author_org_name"] = eitem["org_name"]
        eitem["author_domain"] = eitem["domain"]

        return eitem

    def get_item_project(self, item):
        """ Get project mapping enrichment field """
        ds_name = "scr"  # data source name in projects map
        url = item['origin']
        repo = url+"_"+item['data']['project']
        try:
            project = (self.prjs_map[ds_name][repo])
        except KeyError:
            # logging.warning("Project not found for repository %s" % (repo))
            project = None
        return {"project": project}

    def get_identities(self, item):
        ''' Return the identities from an item '''

        identities = []

        item = item['data']

        # Changeset owner
        user = item['owner']
        identities.append(self.get_sh_identity(user))

        # Patchset uploader and author
        if 'patchSets' in item:
            for patchset in item['patchSets']:
                user = patchset['uploader']
                identities.append(self.get_sh_identity(user))
                if 'author' in patchset:
                    user = patchset['author']
                    identities.append(self.get_sh_identity(user))
                if 'approvals' in patchset:
                    # Approvals by
                    for approval in patchset['approvals']:
                        user = approval['by']
                        identities.append(self.get_sh_identity(user))
        # Comments reviewers
        if 'comments' in item:
            for comment in item['comments']:
                user = comment['reviewer']
                identities.append(self.get_sh_identity(user))

        return identities

    def get_item_id(self, eitem):
        """ Return the item_id linked to this enriched eitem """

        # The eitem _id includes also the patch.
        return eitem["_source"]["review_id"]

    def _fix_review_dates(self, item):
        ''' Convert dates so ES detect them '''


        for date_field in ['timestamp','createdOn','lastUpdated']:
            if date_field in item.keys():
                date_ts = item[date_field]
                item[date_field] = time.strftime('%Y-%m-%dT%H:%M:%S',
                                                  time.localtime(date_ts))
        if 'patchSets' in item.keys():
            for patch in item['patchSets']:
                pdate_ts = patch['createdOn']
                patch['createdOn'] = time.strftime('%Y-%m-%dT%H:%M:%S',
                                                   time.localtime(pdate_ts))
                if 'approvals' in patch:
                    for approval in patch['approvals']:
                        adate_ts = approval['grantedOn']
                        approval['grantedOn'] = \
                            time.strftime('%Y-%m-%dT%H:%M:%S',
                                          time.localtime(adate_ts))
        if 'comments' in item.keys():
            for comment in item['comments']:
                cdate_ts = comment['timestamp']
                comment['timestamp'] = time.strftime('%Y-%m-%dT%H:%M:%S',
                                                     time.localtime(cdate_ts))


    def get_elastic_mappings(self):

        mapping = """
        {
            "properties": {
               "summary_analyzed": {
                  "type": "string",
                  "index":"analyzed"
               },
               "timeopen": {
                  "type": "double"
               }
            }
        }
        """

        return {"items":mapping}


    def review_item(self, item):
        eitem = {}  # Item enriched

        # metadata fields to copy
        copy_fields = ["metadata__updated_on","metadata__timestamp","ocean-unique-id","origin"]
        for f in copy_fields:
            if f in item:
                eitem[f] = item[f]
            else:
                eitem[f] = None
        eitem['closed'] = item['metadata__updated_on']
        # The real data
        review = item['data']
        self._fix_review_dates(review)

        # data fields to copy
        copy_fields = ["status", "branch", "url"]
        for f in copy_fields:
            eitem[f] = review[f]
        # Fields which names are translated
        map_fields = {"subject": "summary",
                      "id": "githash",
                      "createdOn": "opened",
                      "project": "repository",
                      "number": "number"
                      }
        for fn in map_fields:
            eitem[map_fields[fn]] = review[fn]
        eitem["summary_analyzed"] = eitem["summary"]
        eitem["name"] = None
        eitem["domain"] = None
        if 'name' in review['owner']:
            eitem["name"] = review['owner']['name']
            if 'email' in review['owner']:
                if '@' in review['owner']['email']:
                    eitem["domain"] = review['owner']['email'].split("@")[1]
        # New fields generated for enrichment
        eitem["patchsets"] = len(review["patchSets"])

        # Time to add the time diffs
        createdOn_date = parser.parse(review['createdOn'])
        if len(review["patchSets"]) > 0:
            createdOn_date = parser.parse(review["patchSets"][0]['createdOn'])
        seconds_day = float(60*60*24)
        timeopen = \
            (datetime.utcnow()-createdOn_date).total_seconds() / seconds_day
        eitem["timeopen"] =  '%.2f' % timeopen

        if self.sortinghat:
            eitem.update(self.get_item_sh(item))

        if self.prjs_map:
            eitem.update(self.get_item_project(item))

        bulk_json = '{"index" : {"_id" : "%s" } }\n' % (eitem[self.get_field_unique_id()])  # Bulk operation
        bulk_json += json.dumps(eitem)+"\n"

        return bulk_json


    def enrich_items(self, items):
        """ Fetch in ES patches and comments (events) as documents """

        def send_bulk_json(bulk_json, current):
            url_bulk = self.elastic.index_url+'/'+self.type_name+'/_bulk'
            try:
                task_init = time.time()
                requests.put(url_bulk, data=bulk_json)
                logging.debug("bulk packet sent (%.2f sec, %i items)"
                              % (time.time()-task_init, current))
            except UnicodeEncodeError:
                # Why is requests encoding the POST data as ascii?
                logging.error("Unicode error for events in review: " + review['id'])
                safe_json = str(bulk_json.encode('ascii', 'ignore'),'ascii')
                requests.put(url_bulk, data=safe_json)
                # Continue with execution.

        bulk_json = ""  # json data added in bulk operations
        total = 0
        current = 0

        for review in items:
            if current >= self.elastic.max_items_bulk:
                send_bulk_json(bulk_json, current)
                total += current
                current = 0
                bulk_json = ""
            # data_json = self.review_events(review)
            data_json = self.review_item(review)
            bulk_json += data_json +"\n"  # Bulk document
            current += 1
        send_bulk_json(bulk_json, current)
