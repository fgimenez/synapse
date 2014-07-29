# -*- coding: utf-8 -*-
"""Contains functions for performing events on rooms."""
from twisted.internet import defer

from synapse.types import UserID
from synapse.api.constants import Membership
from synapse.api.errors import RoomError, StoreError
from synapse.api.events.room import (
    RoomTopicEvent, MessageEvent, InviteJoinEvent, RoomMemberEvent
)
from synapse.api.streams.event import EventStream, MessagesStreamData
from synapse.util import stringutils
from ._base import BaseHandler

import logging
import json
import time


logger = logging.getLogger(__name__)


class MessageHandler(BaseHandler):

    @defer.inlineCallbacks
    def get_message(self, msg_id=None, room_id=None, sender_id=None,
                    user_id=None):
        """ Retrieve a message.

        Args:
            msg_id (str): The message ID to obtain.
            room_id (str): The room where the message resides.
            sender_id (str): The user ID of the user who sent the message.
            user_id (str): The user ID of the user making this request.
        Returns:
            The message, or None if no message exists.
        Raises:
            SynapseError if something went wrong.
        """
        yield self.auth.check_joined_room(room_id, user_id)

        # Pull out the message from the db
        msg = yield self.store.get_message(room_id=room_id,
                                               msg_id=msg_id,
                                               user_id=sender_id)

        if msg:
            defer.returnValue(msg)
        defer.returnValue(None)

    @defer.inlineCallbacks
    def send_message(self, event=None, suppress_auth=False, stamp_event=True):
        """ Send a message.

        Args:
            event : The message event to store.
            suppress_auth (bool) : True to suppress auth for this message. This
            is primarily so the home server can inject messages into rooms at
            will.
            stamp_event (bool) : True to stamp event content with server keys.
        Raises:
            SynapseError if something went wrong.
        """
        if stamp_event:
            stamp(event)

        with (yield self.room_lock.lock(event.room_id)):
            if not suppress_auth:
                yield self.auth.check(event, raises=True)

            # store message in db
            store_id = yield self.store.persist_event(event)

            event.destinations = yield self.store.get_joined_hosts_for_room(
                event.room_id
            )

            yield self.hs.get_federation().handle_new_event(event)

            self.notifier.on_new_event(event, store_id)

    @defer.inlineCallbacks
    def get_messages(self, user_id=None, room_id=None, pagin_config=None,
                     feedback=False):
        """Get messages in a room.

        Args:
            user_id (str): The user requesting messages.
            room_id (str): The room they want messages from.
            pagin_config (synapse.api.streams.PaginationConfig): The pagination
            config rules to apply, if any.
            feedback (bool): True to get compressed feedback with these messages
        Returns:
            dict: Pagination API results
        """
        yield self.auth.check_joined_room(room_id, user_id)

        data_source = [MessagesStreamData(self.store, room_id=room_id,
                                          feedback=feedback)]
        event_stream = EventStream(user_id, data_source)
        data_chunk = yield event_stream.get_chunk(config=pagin_config)
        defer.returnValue(data_chunk)

    @defer.inlineCallbacks
    def store_room_path_data(self, event=None, path=None, stamp_event=True):
        """ Stores data for a room under a given path.

        Args:
            event : The room path event
            path : The path which can be used to retrieve the data.
            stamp_event (bool) : True to stamp event content with server keys.
        Raises:
            SynapseError if something went wrong.
        """
        yield self.auth.check(event, raises=True)

        if stamp_event:
            stamp(event)

        # store in db
        yield self.store.store_path_data(room_id=event.room_id,
                                         path=path,
                                         content=json.dumps(event.content))

    @defer.inlineCallbacks
    def get_room_path_data(self, user_id=None, room_id=None, path=None,
                           event_type=None,
                           public_room_rules=[],
                           private_room_rules=["join"]):
        """ Get path data from a room.

        Args:
            event : The room path event
            path : The path the data was stored under.
            public_room_rules : A list of membership states the user can be in,
            in order to read this data IN A PUBLIC ROOM. An empty list means
            'any state'.
            private_room_rules : A list of membership states the user can be in,
            in order to read this data IN A PRIVATE ROOM. An empty list means
            'any state'.
        Returns:
            The path data content.
        Raises:
            SynapseError if something went wrong.
        """
        if event_type == RoomTopicEvent.TYPE:
            # anyone invited/joined can read the topic
            private_room_rules = ["invite", "join"]

        # does this room exist
        room = yield self.store.get_room(room_id)
        if not room:
            raise RoomError(403, "Room does not exist.")

        # does this user exist in this room
        member = yield self.store.get_room_member(
            room_id=room_id,
            user_id="" if not user_id else user_id)

        member_state = member.membership if member else None

        if room.is_public and public_room_rules:
            # make sure the user meets public room rules
            if member_state not in public_room_rules:
                raise RoomError(403, "Member does not meet public room rules.")
        elif not room.is_public and private_room_rules:
            # make sure the user meets private room rules
            if member_state not in private_room_rules:
                raise RoomError(
                    403, "Member does not meet private room rules.")

        data = yield self.store.get_path_data(path)
        defer.returnValue(data)

    @defer.inlineCallbacks
    def get_feedback(self, room_id=None, msg_sender_id=None, msg_id=None,
                     user_id=None, fb_sender_id=None, fb_type=None):
        yield self.auth.check_joined_room(room_id, user_id)

        # Pull out the feedback from the db
        fb = yield self.store.get_feedback(
                room_id=room_id, msg_id=msg_id, msg_sender_id=msg_sender_id,
                fb_sender_id=fb_sender_id, fb_type=fb_type)

        if fb:
            defer.returnValue(fb)
        defer.returnValue(None)

    @defer.inlineCallbacks
    def send_feedback(self, event, stamp_event=True):
        if stamp_event:
            stamp(event)

        with (yield self.room_lock.lock(event.room_id)):
            yield self.auth.check(event, raises=True)

            # store message in db
            store_id = yield self.store.persist_event(event)

            event.destinations = yield self.store.get_joined_hosts_for_room(
                event.room_id
            )
        yield self.hs.get_federation().handle_new_event(event)

        self.notifier.on_new_event(event, store_id)


class RoomCreationHandler(BaseHandler):

    @defer.inlineCallbacks
    def create_room(self, user_id=None, room_id=None, config=None):
        """ Creates a new room.

        Args:
            user_id (str): The ID of the user creating the new room.
            room_id (str): The proposed ID for the new room. Can be None, in
            which case one will be created for you.
            config (dict) : A dict of configuration options.
        Returns:
            The new room ID.
        Raises:
            SynapseError if the room ID was taken, couldn't be stored, or
            something went horribly wrong.
        """
        if room_id:
            yield self.store.store_room(
                room_id=room_id,
                room_creator_user_id=user_id,
                is_public=config["visibility"] == "public"
            )
        else:
            # autogen room IDs and try to create it. We may clash, so just
            # try a few times till one goes through, giving up eventually.
            attempts = 0
            room_id = None
            while attempts < 5:
                try:
                    gen_room_id = stringutils.random_string(18)
                    yield self.store.store_room(
                        room_id=gen_room_id,
                        room_creator_user_id=user_id,
                        is_public=config["visibility"] == "public"
                    )
                    room_id = gen_room_id
                    break
                except StoreError:
                    attempts += 1
            if not room_id:
                raise StoreError(500, "Couldn't generate a room ID.")

        content = {"membership": Membership.JOIN}
        join_event = self.event_factory.create_event(
            etype=RoomMemberEvent.TYPE,
            target_user_id=user_id,
            room_id=room_id,
            user_id=user_id,
            membership=Membership.JOIN,
            content=content
        )
        yield self.hs.get_handlers().room_member_handler.change_membership(
            join_event,
            broadcast_msg=True,
            do_auth=False
        )

        defer.returnValue(room_id)


class RoomMemberHandler(BaseHandler):

    @defer.inlineCallbacks
    def get_room_members(self, room_id=None, user_id=None, limit=0,
                         start_tok=None, end_tok=None):
        """Retrieve a list of room members in the room.

        Args:
            room_id (str): The room to get the member list for.
            user_id (str): The ID of the user making the request.
            limit (int): The max number of members to return.
            start_tok (str): Optional. The start token if known.
            end_tok (str): Optional. The end token if known.
        Returns:
            dict: A Pagination streamable dict.
        Raises:
            SynapseError if something goes wrong.
        """
        yield self.auth.check_joined_room(room_id, user_id)

        member_list = yield self.store.get_room_members(room_id=room_id)
        event_list = self.store.to_events(member_list)
        chunk_data = {
            "start": "START",
            "end": "END",
            "chunk": event_list
        }
        # TODO honor Pagination stream params
        # TODO snapshot this list to return on subsequent requests when
        # paginating
        defer.returnValue(chunk_data)

    @defer.inlineCallbacks
    def get_room_member(self, room_id, member_user_id, auth_user_id):
        """Retrieve a room member from a room.

        Args:
            room_id : The room the member is in.
            member_user_id : The member's user ID
            auth_user_id : The user ID of the user making this request.
        Returns:
            The room member, or None if this member does not exist.
        Raises:
            SynapseError if something goes wrong.
        """
        yield self.auth.check_joined_room(room_id, auth_user_id)

        member = yield self.store.get_room_member(user_id=member_user_id,
                                                  room_id=room_id)
        defer.returnValue(member)

    @defer.inlineCallbacks
    def change_membership(self, event=None, broadcast_msg=False, do_auth=True):
        """ Change the membership status of a user in a room.

        Args:
            event (SynapseEvent): The membership event
            broadcast_msg (bool): True to inject a membership message into this
                room on success.
        Raises:
            SynapseError if there was a problem changing the membership.
        """

        #broadcast_msg = False

        @defer.inlineCallbacks
        def _do_membership_update():
            # store membership
            store_id = yield self.store.store_room_member(
                user_id=event.target_user_id,
                sender=event.user_id,
                room_id=event.room_id,
                content=event.content,
                membership=event.content["membership"]
            )

            # Send a PDU to all hosts who have joined the room.
            destinations = yield self.store.get_joined_hosts_for_room(
                event.room_id
            )

            # If we're inviting someone, then we should also send it to that
            # HS.
            if event.content["membership"] == Membership.INVITE:
                host = UserID.from_string(
                    event.target_user_id, self.hs
                ).domain
                destinations.append(host)

            # If we are joining a remote HS, include that.
            if event.content["membership"] == Membership.JOIN:
                host = UserID.from_string(
                    event.target_user_id, self.hs
                ).domain
                destinations.append(host)

            event.destinations = list(set(destinations))

            defer.returnValue(store_id)

        # If we're trying to join a room then we have to do this differently
        # if this HS is not currently in the room, i.e. we have to do the
        # invite/join dance.
        if event.membership == Membership.JOIN:
            with (yield self.room_lock.lock(event.room_id)):
                if do_auth:
                    yield self.auth.check(event, raises=True)

                prev_state = yield self.store.get_room_member(
                    event.target_user_id, event.room_id
                )

                # Only do an invite join dance if a) we were invited,
                # b) the person inviting was from a differnt HS and c) we are
                # not currently in the room
                if prev_state and prev_state.membership == Membership.INVITE:
                    room = yield self.store.get_room(event.room_id)
                    inviter = UserID.from_string(
                        prev_state.sender, self.hs
                    )

                    is_remote_invite_join = not inviter.is_mine and not room
                else:
                    is_remote_invite_join = False

                # We want to do the _do_update inside the room lock.
                if not is_remote_invite_join:
                    logger.debug("Doing normal join")
                    yield self.state_handler.handle_new_event(event)
                    store_id = yield _do_membership_update()

            # We don't want to send out the event notifs inside the room lock.
            if not is_remote_invite_join:
                yield self.hs.get_federation().handle_new_event(event)
                self.notifier.on_new_event(event, store_id)

            else:
                logger.debug("Doing remote join dance")

                yield self.store.store_room(
                    event.room_id, str(inviter), is_public=False
                )

                # do invite join dance
                federation = self.hs.get_federation()
                new_event = self.event_factory.create_event(
                    etype=InviteJoinEvent.TYPE,
                    target_user_id=inviter.to_string(),
                    room_id=event.room_id,
                    user_id=event.user_id,
                    content={}
                )

                new_event.destinations = [inviter.domain]

                #yield self.state_handler.handle_new_event(event)
                yield federation.handle_new_event(new_event)
                yield federation.get_state_for_room(
                    inviter.domain, event.room_id
                )

        else:
            # This is not a JOIN, so we can handle it normally.
            with (yield self.room_lock.lock(event.room_id)):
                if do_auth:
                    yield self.auth.check(event, raises=True)

                yield self.state_handler.handle_new_event(event)
                store_id = yield _do_membership_update()

            yield self.hs.get_federation().handle_new_event(event)
            self.notifier.on_new_event(event, store_id)

        if broadcast_msg:
            yield self._inject_membership_msg(
                source=event.user_id,
                target=event.target_user_id,
                room_id=event.room_id,
                membership=event.content["membership"])

    @defer.inlineCallbacks
    def _inject_membership_msg(self, room_id=None, source=None, target=None,
                               membership=None):
        # TODO this should be a different type of message, not sy.text
        if membership == Membership.INVITE:
            body = "%s invited %s to the room." % (source, target)
        elif membership == Membership.JOIN:
            body = "%s joined the room." % (target)
        elif membership == Membership.LEAVE:
            body = "%s left the room." % (target)
        else:
            raise RoomError(500, "Unknown membership value %s" % membership)

        membership_json = {
            "msgtype": u"sy.text",
            "body": body
        }
        msg_id = "m%s" % int(time.time() * 1000)

        event = self.event_factory.create_event(
                etype=MessageEvent.TYPE,
                room_id=room_id,
                user_id="_homeserver_",
                msg_id=msg_id,
                content=membership_json
                )

        handler = self.hs.get_handlers().message_handler
        yield handler.send_message(event, suppress_auth=True)


def stamp(event):
    event.content["hsob_ts"] = int(time.time() * 1000)