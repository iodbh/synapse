# -*- coding: utf-8 -*-
# Copyright 2015 OpenMarket Ltd
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

import logging

from twisted.internet import defer

from .push_rule_evaluator import PushRuleEvaluatorForEvent

from synapse.visibility import filter_events_for_clients_context
from synapse.api.constants import EventTypes, Membership
from synapse.util.caches.descriptors import cached


logger = logging.getLogger(__name__)


rules_by_room = {}


class BulkPushRuleEvaluator:
    """Calculates the outcome of push rules for an event for all users in the
    room at once.
    """

    def __init__(self, hs):
        self.hs = hs
        self.store = hs.get_datastore()

    @defer.inlineCallbacks
    def _get_rules_for_event(self, event, context):
        room_id = event.room_id
        rules_for_room = self._get_rules_for_room(room_id)

        rules_by_user = yield rules_for_room.get_rules(context)

        # if this event is an invite event, we may need to run rules for the user
        # who's been invited, otherwise they won't get told they've been invited
        if event.type == 'm.room.member' and event.content['membership'] == 'invite':
            invited = event.state_key
            if invited and self.hs.is_mine_id(invited):
                has_pusher = yield self.store.user_has_pusher(invited)
                if has_pusher:
                    rules_by_user = dict(rules_by_user)
                    rules_by_user[invited] = yield self.store.get_push_rules_for_user(
                        invited
                    )

        defer.returnValue(rules_by_user)

    @cached(max_entries=10000)
    def _get_rules_for_room(self, room_id):
        return RulesForRoom(self.hs, room_id, self._get_rules_for_room.cache)

    @defer.inlineCallbacks
    def action_for_event_by_user(self, event, context):
        rules_by_user = yield self._get_rules_for_event(event, context)
        actions_by_user = {}

        # None of these users can be peeking since this list of users comes
        # from the set of users in the room, so we know for sure they're all
        # actually in the room.
        user_tuples = [
            (u, False) for u in rules_by_user.iterkeys()
        ]

        filtered_by_user = yield filter_events_for_clients_context(
            self.store, user_tuples, [event], {event.event_id: context}
        )

        room_members = yield self.store.get_joined_users_from_context(
            event, context
        )

        logger.info("Room members: %d", len(room_members))
        logger.info("Rules: %r", rules_by_user)

        evaluator = PushRuleEvaluatorForEvent(event, len(room_members))

        condition_cache = {}

        for uid, rules in rules_by_user.iteritems():
            logger.info("Calculating push for %r, rules: %r", rules)

            display_name = None
            profile_info = room_members.get(uid)
            if profile_info:
                display_name = profile_info.display_name

            if not display_name:
                # Handle the case where we are pushing a membership event to
                # that user, as they might not be already joined.
                if event.type == EventTypes.Member and event.state_key == uid:
                    display_name = event.content.get("displayname", None)

            filtered = filtered_by_user[uid]
            if len(filtered) == 0:
                continue

            if filtered[0].sender == uid:
                continue

            for rule in rules:
                if 'enabled' in rule and not rule['enabled']:
                    continue

                matches = _condition_checker(
                    evaluator, rule['conditions'], uid, display_name, condition_cache
                )
                if matches:
                    actions = [x for x in rule['actions'] if x != 'dont_notify']
                    if actions and 'notify' in actions:
                        actions_by_user[uid] = actions
                    break
        defer.returnValue(actions_by_user)


def _condition_checker(evaluator, conditions, uid, display_name, cache):
    for cond in conditions:
        _id = cond.get("_id", None)
        if _id:
            res = cache.get(_id, None)
            if res is False:
                return False
            elif res is True:
                continue

        res = evaluator.matches(cond, uid, display_name)
        if _id:
            cache[_id] = bool(res)

        if not res:
            return False

    return True


class RulesForRoom(object):
    """Caches push rules for users in a room.

    This efficiently handles users joining/leaving the room by not invalidating
    the entire cache for the room.
    """

    def __init__(self, hs, room_id, rules_for_room_cache):
        """
        Args:
            hs (HomeServer)
            room_id (str)
            rules_for_room_cache(Cache): The cache object that caches these
                RoomsForUser objects.
        """
        self.room_id = room_id
        self.is_mine_id = hs.is_mine_id
        self.store = hs.get_datastore()

        self.member_map = {}  # event_id -> (user_id, state)
        self.rules_by_user = {}  # user_id -> rules

        # The last state group we updated the caches for. If the state_group of
        # a new event comes along, we know that we can just return the cached
        # result.
        # On invalidation of the rules themselves (if the user changes them),
        # we invalidate everything and set state_group to `object()`
        self.state_group = object()

        # A sequence number to keep track of when we're allowed to update the
        # cache. We bump the sequence number when we invalidate the cache. If
        # the sequence number changes while we're calculating stuff we should
        # not update the cache with it.
        self.sequence = 0

        # We need to be clever on the invalidating caches callbacks, as
        # otherwise the invalidation callback holds a reference to the object,
        # potentially causing it to leak.
        # To get around this we pass a function that on invalidations looks ups
        # the RoomsForUser entry in the cache, rather than keeping a reference
        # to self around in the callback.
        def invalidate_all_cb():
            rules = rules_for_room_cache.get(room_id, update_metrics=False)
            if rules:
                rules.invalidate_all()

        self.invalidate_all_cb = invalidate_all_cb

    @defer.inlineCallbacks
    def get_rules(self, context):
        # TODO: Remove left users? And don't return them.

        state_group = context.state_group
        current_state_ids = context.current_state_ids

        if state_group and self.state_group == state_group:
            defer.returnValue(self.rules_by_user)

        ret_rules_by_user = {}
        missing_member_event_ids = {}
        for key, event_id in current_state_ids.iteritems():
            res = self.member_map.get(event_id, None)
            if res:
                user_id, state = res
                if state == Membership.JOIN:
                    rules = self.rules_by_user.get(user_id, None)
                    if rules:
                        ret_rules_by_user[user_id] = rules
                continue

            if key[0] != EventTypes.Member:
                continue

            user_id = key[1]
            if not self.is_mine_id(user_id):
                continue

            if self.store.get_if_app_services_interested_in_user(user_id):
                continue

            missing_member_event_ids[user_id] = event_id

        if missing_member_event_ids:
            missing_rules = yield self._get_rules_for_member_event_ids(
                missing_member_event_ids, state_group
            )
            ret_rules_by_user.update(missing_rules)

        defer.returnValue(ret_rules_by_user)

    @defer.inlineCallbacks
    def _get_rules_for_member_event_ids(self, member_event_ids, state_group):
        sequence = self.sequence

        rows = yield self.store._simple_select_many_batch(
            table="room_memberships",
            column="event_id",
            iterable=member_event_ids.values(),
            retcols=['user_id', 'membership', 'event_id'],
            keyvalues={},
            batch_size=500,
            desc="_get_rules_for_member_event_ids",
        )

        members = {
            row["event_id"]: (row["user_id"], row["membership"])
            for row in rows
        }

        interested_in_user_ids = set(user_id for user_id, _ in members.itervalues())

        if_users_with_pushers = yield self.store.get_if_users_have_pushers(
            interested_in_user_ids,
            on_invalidate=self.invalidate_all_cb,
        )

        user_ids = set(
            uid for uid, have_pusher in if_users_with_pushers.items() if have_pusher
        )

        users_with_receipts = yield self.store.get_users_with_read_receipts_in_room(
            self.room_id, on_invalidate=self.invalidate_all_cb,
        )

        # any users with pushers must be ours: they have pushers
        for uid in users_with_receipts:
            if uid in interested_in_user_ids:
                user_ids.add(uid)

        rules_by_user = yield self.store.bulk_get_push_rules(
            user_ids, on_invalidate=self.invalidate_all_cb,
        )

        rules_by_user = {k: v for k, v in rules_by_user.iteritems() if v is not None}

        self.update_cache(sequence, members, rules_by_user, state_group)

        defer.returnValue(rules_by_user)

    def invalidate_all(self):
        # XXX: LEAK!!! As things will hold on to a reference to this
        self.sequence += 1
        self.state_group = object()
        self.member_map = {}
        self.rules_by_user = {}

    def update_cache(self, sequence, members, rules_by_user, state_group):
        if sequence == self.sequence:
            self.member_map.update(members)
            self.rules_by_user.update(rules_by_user)
            self.state_group = state_group
