# The contents of this file are subject to the Common Public Attribution
# License Version 1.0. (the "License"); you may not use this file except in
# compliance with the License. You may obtain a copy of the License at
# http://code.reddit.com/LICENSE. The License is based on the Mozilla Public
# License Version 1.1, but Sections 14 and 15 have been added to cover use of
# software over a computer network and provide for limited attribution for the
# Original Developer. In addition, Exhibit A has been modified to be consistent
# with Exhibit B.
#
# Software distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTY OF ANY KIND, either express or implied. See the License for
# the specific language governing rights and limitations under the License.
#
# The Original Code is reddit.
#
# The Original Developer is the Initial Developer.  The Initial Developer of
# the Original Code is reddit Inc.
#
# All portions of the code written by reddit are Copyright (c) 2006-2015 reddit
# Inc. All Rights Reserved.
###############################################################################

from datetime import datetime
import json

from pylons import tmpl_context as c, app_globals as g, request

from r2.lib import amqp, hooks
from r2.lib.eventcollector import Event
from r2.lib.utils import epoch_timestamp
from r2.models import Account, Comment, Link
from r2.models.last_modified import LastModified
from r2.models.vote import Vote, VotesByAccount

from r2.lib.geoip import organization_by_ips

def prequeued_vote_key(user, item):
    return 'queuedvote:%s_%s' % (user._id36, item._fullname)


def update_vote_lookups(user, thing, direction):
    """Store info about the existence of this vote (before processing)."""
    # set the vote in memcached so the UI gets updated immediately
    key = prequeued_vote_key(user, thing)
    grace_period = int(g.vote_queue_grace_period.total_seconds())
    direction = Vote.serialize_direction(direction)
    g.gencache.set(key, direction, time=grace_period+1)

    # update LastModified immediately to help us cull prequeued_vote lookups
    rel_cls = VotesByAccount.rel(thing.__class__)
    LastModified.touch(user._fullname, rel_cls._last_modified_name)


def cast_vote(user, thing, direction, **data):
    """Register a vote and queue it for processing."""
    update_vote_lookups(user, thing, direction)

    vote_data = {
        "user_id": user._id,
        "thing_fullname": thing._fullname,
        "direction": direction,
        "date": int(epoch_timestamp(datetime.now(g.tz))),
    }

    data['ip'] = getattr(request, "ip", None)
    if data['ip'] is not None:
        data['org'] = organization_by_ips(data['ip'])
    vote_data['data'] = data

    hooks.get_hook("vote.get_vote_data").call(
        data=vote_data["data"],
        user=user,
        thing=thing,
        request=request,
        context=c,
    )

    # The vote event will actually be sent from an async queue processor, so
    # we need to pull out the context data at this point
    if not g.running_as_script:
        vote_data["event_data"] = {
            "context": Event.get_context_data(request, c),
            "sensitive": Event.get_sensitive_context_data(request, c),
        }

    amqp.add_item(thing.vote_queue_name, json.dumps(vote_data))


def consume_link_vote_queue(qname="vote_link_q"):
    @g.stats.amqp_processor(qname)
    def process_message(msg):
        from r2.lib.db.queries import new_link_vote

        timer = g.stats.get_timer("new_voting.%s" % qname)
        timer.start()

        vote_data = json.loads(msg.body)
        hook = hooks.get_hook('vote.validate_vote_data')
        if hook.call_until_return(msg=msg, vote_data=vote_data) is False:
            # Corrupt records in the queue. Ignore them.
            print "Ignoring invalid vote by %s on %s %s" % (
                    vote_data.get('user_id', '<unknown>'),
                    vote_data.get('thing_fullname', '<unknown>'),
                    vote_data)
            return

        user = Account._byID(vote_data.pop("user_id"))
        link = Link._by_fullname(vote_data.pop("thing_fullname"))
        timer.intermediate("preamble")

        lock_key = "vote-%s-%s" % (user._id36, link._fullname)
        with g.make_lock("voting", lock_key, timeout=5):
            print "Processing vote by %s on %s %s" % (user, link, vote_data)

            try:
                vote = Vote(
                    user,
                    link,
                    direction=vote_data["direction"],
                    date=datetime.utcfromtimestamp(vote_data["date"]),
                    data=vote_data["data"],
                    event_data=vote_data.get("event_data"),
                )
            except TypeError as e:
                # a vote on an invalid type got in the queue, just skip it
                g.log.exception("Invalid type: %r", e.message)
                return

            timer.intermediate("create_vote_obj")

            vote.commit()

            new_link_vote(vote)

            timer.flush()

    amqp.consume_items(qname, process_message, verbose=False)


def consume_comment_vote_queue(qname="vote_comment_q"):
    @g.stats.amqp_processor(qname)
    def process_message(msg):
        from r2.lib.db.queries import new_comment_vote

        timer = g.stats.get_timer("new_voting.%s" % qname)
        timer.start()

        vote_data = json.loads(msg.body)
        hook = hooks.get_hook('vote.validate_vote_data')
        if hook.call_until_return(msg=msg, vote_data=vote_data) is False:
            # Corrupt records in the queue. Ignore them.
            print "Ignoring invalid vote by %s on %s %s" % (
                    vote_data.get('user_id', '<unknown>'),
                    vote_data.get('thing_fullname', '<unknown>'),
                    vote_data)
            return

        user = Account._byID(vote_data.pop("user_id"))
        comment = Comment._by_fullname(vote_data.pop("thing_fullname"))
        timer.intermediate("preamble")

        lock_key = "vote-%s-%s" % (user._id36, comment._fullname)
        with g.make_lock("voting", lock_key, timeout=5):
            print "Processing vote by %s on %s %s" % (user, comment, vote_data)

            try:
                vote = Vote(
                    user,
                    comment,
                    direction=vote_data["direction"],
                    date=datetime.utcfromtimestamp(vote_data["date"]),
                    data=vote_data["data"],
                    event_data=vote_data.get("event_data"),
                )
            except TypeError as e:
                # a vote on an invalid type got in the queue, just skip it
                g.log.exception("Invalid type: %r", e.message)
                return

            timer.intermediate("create_vote_obj")

            vote.commit()

            new_comment_vote(vote)

            timer.flush()

    amqp.consume_items(qname, process_message, verbose=False)
