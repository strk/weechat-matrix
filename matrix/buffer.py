# -*- coding: utf-8 -*-

# Weechat Matrix Protocol Script
# Copyright © 2018 Damir Jelić <poljar@termina.org.uk>
#
# Permission to use, copy, modify, and/or distribute this software for
# any purpose with or without fee is hereby granted, provided that the
# above copyright notice and this permission notice appear in all copies.
#
# THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
# WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
# MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
# SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER
# RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF
# CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR IN
# CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.

from __future__ import unicode_literals

import time
from builtins import super
from functools import partial

from .globals import W, SERVERS, OPTIONS, SCRIPT_NAME
from .utf import utf8_decode
from .colors import Formatted
from .utils import shorten_sender, server_ts_to_weechat, string_strikethrough
from .plugin_options import RedactType


from .rooms import (
    RoomNameEvent,
    RoomAliasEvent,
    RoomMembershipEvent,
    RoomMemberJoin,
    RoomMemberLeave,
    RoomMemberInvite,
    RoomTopicEvent,
    RoomMessageText,
    RoomMessageEmote,
    RoomRedactionEvent,
    RoomRedactedMessageEvent
)


@utf8_decode
def room_buffer_input_cb(server_name, buffer, input_data):
    server = SERVERS[server_name]
    room, room_buffer = server.find_room_from_ptr(buffer)

    if not room_buffer:
        # TODO log error
        return

    if not server.connected:
        room_buffer.error("You are not connected to the server")
        return W.WEECHAT_RC_ERROR

    formatted_data = Formatted.from_input_line(input_data)

    server.send_room_message(room, formatted_data)

    return W.WEECHAT_RC_OK


@utf8_decode
def room_buffer_close_cb(data, buffer):
    return W.WEECHAT_RC_OK


class WeechatUser(object):
    def __init__(self, nick, host=None, prefix=""):
        # type: (str, str, str) -> None
        self.nick = nick
        self.host = host
        self.prefix = prefix
        self.color = W.info_get("nick_color_name", nick)


class RoomUser(WeechatUser):
    def __init__(self, nick, user_id=None, power_level=0):
        # type: (str, str, int) -> None
        prefix = self._get_prefix(power_level)
        return super().__init__(nick, user_id, prefix)

    @staticmethod
    def _get_prefix(power_level):
        # type: (int) -> str
        if power_level >= 100:
            return "&"
        elif power_level >= 50:
            return "@"
        elif power_level > 0:
            return "+"
        return ""


class WeechatChannelBuffer(object):
    tags = {
        "message": [
            SCRIPT_NAME + "_message",
            "notify_message",
            "log1"
        ],
        "self_message": [
            SCRIPT_NAME + "_message",
            "notify_none",
            "no_highlight",
            "self_msg",
            "log1"
        ],
        "action": [
            SCRIPT_NAME + "_message",
            SCRIPT_NAME + "_action",
            "notify_message",
            "log1",
        ],
        "old_message": [
            SCRIPT_NAME + "_message",
            "notify_message",
            "no_log",
            "no_highlight"
        ],
        "join": [
            SCRIPT_NAME + "_join",
            "log4"
        ],
        "part": [
            SCRIPT_NAME + "_leave",
            "log4"
        ],
        "kick": [
            SCRIPT_NAME + "_kick",
            "log4"
        ],
        "invite": [
            SCRIPT_NAME + "_invite",
            "log4"
        ],
        "topic": [
            SCRIPT_NAME + "_topic",
            "log3",
        ]
    }

    membership_messages = {
        "join": "has joined",
        "part": "has left",
        "kick": "has been kicked from",
        "invite": "has been invited to"
    }

    class Line(object):
        def __init__(self, pointer):
            self._ptr = pointer

        @property
        def _hdata(self):
            return W.hdata_get("line_data")

        @property
        def prefix(self):
            return W.hdata_string(self._hdata, self._ptr, "prefix")

        @prefix.setter
        def prefix(self, new_prefix):
            new_data = {"prefix": new_prefix}
            W.hdata_update(self._hdata, self._ptr, new_data)

        @property
        def message(self):
            return W.hdata_string(self._hdata, self._ptr, "message")

        @message.setter
        def message(self, new_message):
            # type: (str) -> None
            new_data = {"message": new_message}
            W.hdata_update(self._hdata, self._ptr, new_data)

        @property
        def tags(self):
            tags_count = W.hdata_get_var_array_size(
                self._hdata,
                self._ptr,
                "tags_array"
            )

            tags = [
                W.hdata_string(self._hdata, self._ptr, "%d|tags_array" % i)
                for i in range(tags_count)
            ]
            return tags

        @tags.setter
        def tags(self, new_tags):
            # type: (List[str]) -> None
            new_data = {"tags_array": ",".join(new_tags)}
            W.hdata_update(self._hdata, self._ptr, new_data)

        @property
        def date(self):
            # type: () -> int
            return W.hdata_time(self._hdata, self._ptr, "date")

        @date.setter
        def date(self, new_date):
            # type: (int) -> None
            new_data = {"date": new_date}
            W.hdata_update(self._hdata, self._ptr, new_data)

        @property
        def date_printed(self):
            # type: () -> int
            return W.hdata_time(self._hdata, self._ptr, "date_printed")

        @date_printed.setter
        def date_printed(self, new_date):
            # type: (int) -> None
            new_data = {"date_printed": new_date}
            W.hdata_update(self._hdata, self._ptr, new_data)

        @property
        def highlight(self):
            # type: () -> bool
            return bool(W.hdata_char(self._hdata, self._ptr, "highlight"))

        def update(self, date, date_printed, tags, prefix, message):
            new_data = {
                "date": date,
                "date_printed": date_printed,
                "tags_array": ','.join(tags),
                "prefix": prefix,
                "message": message,
                # "highlight": highlight
            }
            W.hdata_update(self._hdata, self._ptr, new_data)

    def __init__(self, name, server_name, user):
        # type: (str, str, str)
        self._ptr = W.buffer_new(
            name,
            "room_buffer_input_cb",
            server_name,
            "room_buffer_close_cb",
            server_name,
        )

        self.name = ""
        self.users = {}  # type: Dict[str, RoomUser]

        self.topic_author = ""
        self.topic_date = None

        W.buffer_set(self._ptr, "localvar_set_type", 'channel')
        W.buffer_set(self._ptr, "type", 'formatted')

        W.buffer_set(self._ptr, "localvar_set_channel", name)

        W.buffer_set(self._ptr, "localvar_set_nick", user)

        W.buffer_set(self._ptr, "localvar_set_server", server_name)

        # short_name = strip_matrix_server(room_id)
        # W.buffer_set(self._ptr, "short_name", short_name)

        W.nicklist_add_group(
            self._ptr,
            '',
            "000|o",
            "weechat.color.nicklist_group",
            1
        )
        W.nicklist_add_group(
            self._ptr,
            '',
            "001|h",
            "weechat.color.nicklist_group",
            1
        )
        W.nicklist_add_group(
            self._ptr,
            '',
            "002|v",
            "weechat.color.nicklist_group",
            1
        )
        W.nicklist_add_group(
            self._ptr,
            '',
            "999|...",
            "weechat.color.nicklist_group",
            1
        )

        W.buffer_set(self._ptr, "nicklist", "1")
        W.buffer_set(self._ptr, "nicklist_display_groups", "0")

        W.buffer_set(self._ptr, "highlight_words", user)

        # TODO make this configurable
        W.buffer_set(
            self._ptr,
            "highlight_tags_restrict",
            SCRIPT_NAME + "_message"
        )

    @property
    def _hdata(self):
        return W.hdata_get("buffer")

    @property
    def lines(self):
        own_lines = W.hdata_pointer(
            self._hdata,
            self._ptr,
            "own_lines"
        )

        if own_lines:
            hdata_line = W.hdata_get("line")

            line_pointer = W.hdata_pointer(
                W.hdata_get("lines"), own_lines, "last_line")

            while line_pointer:
                data_pointer = W.hdata_pointer(
                    hdata_line,
                    line_pointer,
                    "data"
                )

                if data_pointer:
                    yield WeechatChannelBuffer.Line(data_pointer)

                line_pointer = W.hdata_move(hdata_line, line_pointer, -1)

    def _print(self, string):
        # type: (str) -> None
        """ Print a string to the room buffer """
        W.prnt(self._ptr, string)

    def print_date_tags(self, data, date=None, tags=None):
        # type: (str, Optional[int], Optional[List[str]]) -> None
        date = date or int(time.time())
        tags = tags or []

        tags_string = ",".join(tags)
        W.prnt_date_tags(self._ptr, date, tags_string, data)

    def error(self, string):
        # type: (str) -> None
        """ Print an error to the room buffer """
        message = "{prefix}{script}: {message}".format(
            W.prefix("error"),
            SCRIPT_NAME,
            string
        )

        self._print(message)

    @staticmethod
    def _color_for_tags(color):
        # type: (str) -> str
        if color == "weechat.color.chat_nick_self":
            option = W.config_get(color)
            return W.config_string(option)

        return color

    def _message_tags(self, user, message_type):
        # type: (str, RoomUser, str) -> List[str]
        tags = list(self.tags[message_type])

        tags.append("nick_{nick}".format(nick=user.nick))

        color = self._color_for_tags(user.color)

        if message_type != "action":
            tags.append("prefix_nick_{color}".format(color=color))

        return tags

    def _get_user(self, nick):
        # type: (str) -> RoomUser
        if nick in self.users:
            return self.users[nick]

        # A message from a non joined user
        return RoomUser(nick)

    def _print_message(self, user, message, date, tags):
        prefix_string = ("" if not user.prefix else "{}{}{}".format(
            W.color(self._get_prefix_color(user.prefix)),
            user.prefix,
            W.color("reset")
        ))

        data = "{prefix}{color}{author}{ncolor}\t{msg}".format(
            prefix=prefix_string,
            color=W.color(user.color),
            author=user.nick,
            ncolor=W.color("reset"),
            msg=message)

        self.print_date_tags(data, date, tags)

    def message(self, nick, message, date, extra_tags=[]):
        # type: (str, str, int, str) -> None
        user = self._get_user(nick)
        tags = self._message_tags(user, "message") + extra_tags
        self._print_message(user, message, date, tags)

    def notice(self, nick, message, date):
        # type: (str, str, int) -> None
        data = "{color}{message}{ncolor}".format(
            color=W.color("irc.color.notice"),
            message=message,
            ncolor=W.color("reset"))

        self.message(nick, data, date)

    def _print_action(self, user, message, date, tags):
        nick_prefix = ("" if not user.prefix else "{}{}{}".format(
            W.color(self._get_prefix_color(user.prefix)),
            user.prefix,
            W.color("reset")
        ))

        data = ("{prefix}{nick_prefix}{nick_color}{author}"
                "{ncolor} {msg}").format(
            prefix=W.prefix("action"),
            nick_prefix=nick_prefix,
            nick_color=W.color(user.color),
            author=user.nick,
            ncolor=W.color("reset"),
            msg=message)

        self.print_date_tags(data, date, tags)

    def action(self, nick, message, date, extra_tags=[]):
        # type: (str, str, int) -> None
        user = self._get_user(nick)
        tags = self._message_tags(user, "action") + extra_tags
        self._print_action(user, message, date, tags)

    @staticmethod
    def _get_nicklist_group(user):
        # type: (WeechatUser) -> str
        group_name = "999|..."

        if user.prefix == "&":
            group_name = "000|o"
        elif user.prefix == "@":
            group_name = "001|h"
        elif user.prefix > "+":
            group_name = "002|v"

        return group_name

    @staticmethod
    def _get_prefix_color(prefix):
        # type: (str) -> str
        # TODO make this configurable
        color = ""

        if prefix == "&":
            color = "lightgreen"
        elif prefix == "@":
            color = "lightgreen"
        elif prefix == "+":
            color = "yellow"

        return color

    def _add_user_to_nicklist(self, user):
        # type: (WeechatUser) -> None
        nick_pointer = W.nicklist_search_nick(self._ptr, "", user.nick)

        if not nick_pointer:
            group = W.nicklist_search_group(
                self._ptr,
                "",
                self._get_nicklist_group(user)
            )
            prefix = user.prefix if user.prefix else " "
            W.nicklist_add_nick(
                self._ptr,
                group,
                user.nick,
                user.color,
                prefix,
                self._get_prefix_color(user.prefix),
                1
            )

    def _membership_message(self, user, message_type):
        # type: (WeechatUser, str) -> str
        action_color = ("green" if message_type == "join"
                        or message_type == "invite" else "red")
        prefix = ("join" if message_type == "join" or message_type == "invite"
                  else "quit")

        membership_message = self.membership_messages[message_type]

        message = ("{prefix}{color}{author}{ncolor} "
                   "{del_color}({host_color}{host}{del_color})"
                   "{action_color} {message} "
                   "{channel_color}{room}{ncolor}").format(
            prefix=W.prefix(prefix),
            color=W.color(user.color),
            author=user.nick,
            ncolor=W.color("reset"),
            del_color=W.color("chat_delimiters"),
            host_color=W.color("chat_host"),
            host=user.host,
            action_color=W.color(action_color),
            message=membership_message,
            channel_color=W.color("chat_channel"),
            room=self.short_name)

        return message

    def join(self, user, date, message=True, extra_tags=[]):
        # type: (WeechatUser, int, Optional[bool], Optional[List[str]]) -> None
        self._add_user_to_nicklist(user)
        self.users[user.nick] = user

        if message:
            tags = self._message_tags(user, "join")
            message = self._membership_message(user, "join")
            self.print_date_tags(message, date, tags)

    def invite(self, nick, date, extra_tags=[]):
        # type: (str, int, Optional[bool], Optional[List[str]]) -> None
        user = self._get_user(nick)
        tags = self._message_tags(user, "invite")
        message = self._membership_message(user, "invite")
        self.print_date_tags(message, date, tags + extra_tags)

    def _remove_user_from_nicklist(self, user):
        # type: (WeechatUser) -> None
        nick_pointer = W.nicklist_search_nick(self._ptr, "", user.nick)

        if nick_pointer:
            W.nicklist_remove_nick(self._ptr, nick_pointer)

    def _leave(self, nick, date, message, leave_type, extra_tags):
        # type: (str, int, bool, str, List[str]) -> None
        user = self._get_user(nick)
        self._remove_user_from_nicklist(user)

        if message:
            tags = self._message_tags(user, leave_type)
            message = self._membership_message(user, leave_type)
            self.print_date_tags(message, date, tags + extra_tags)

        if user.nick in self.users:
            del self.users[user.nick]

    def part(self, nick, date, message=True, extra_tags=[]):
        # type: (str, int, Optional[bool], Optional[List[str]]) -> None
        self._leave(nick, date, message, "part", extra_tags)

    def kick(self, nick, date, message=True, extra_tags=[]):
        # type: (str, int, Optional[bool], Optional[List[str]]) -> None
        self._leave(nick, date, message, "kick", extra_tags=[])

    def _print_topic(self, nick, topic, date):
        user = self._get_user(nick)
        tags = self._message_tags(user, "topic")

        data = ("{prefix}{nick} has changed "
                "the topic for {chan_color}{room}{ncolor} "
                "to \"{topic}\"").format(
                    prefix=W.prefix("network"),
                    nick=user.nick,
                    chan_color=W.color("chat_channel"),
                    ncolor=W.color("reset"),
                    room=self.short_name,
                    topic=topic
                )

        self.print_date_tags(data, date, tags)

    @property
    def topic(self):
        return W.buffer_get_string(self._ptr, "title")

    @topic.setter
    def topic(self, topic):
        W.buffer_set(self._ptr, "title", topic)

    def change_topic(self, nick, topic, date, message=True):
        if message:
            self._print_topic(nick, topic, date)

        self.topic = topic
        self.topic_author = nick
        self.topic_date = date

    def self_message(self, nick, message, date):
        user = self._get_user(nick)
        tags = self._message_tags(user, "self_message")
        self._print_message(user, message, date, tags)

    def self_action(self, nick, message, date):
        user = self._get_user(nick)
        tags = self._message_tags(user, "self_message")
        tags.append(SCRIPT_NAME + "_action")
        self._print_action(user, message, date, tags)

    @property
    def short_name(self):
        return W.buffer_get_string(self._ptr, "short_name")

    @short_name.setter
    def short_name(self, name):
        W.buffer_set(self._ptr, "short_name", name)

    def find_lines(self, predicate):
        lines = []
        for line in self.lines:
            if predicate(line):
                lines.append(line)

        return lines


class RoomBuffer(object):
    def __init__(self, room, server_name):
        self.room = room
        user = shorten_sender(self.room.own_user_id)
        self.weechat_buffer = WeechatChannelBuffer(
            room.room_id,
            server_name,
            user
        )

    def handle_membership_events(self, event, is_state):
        def join(event, date, is_state):
            user = self.room.users[event.sender]
            buffer_user = RoomUser(user.name, event.sender)
            # TODO remove this duplication
            user.nick_color = buffer_user.color

            if self.room.own_user_id == event.sender:
                buffer_user.color = "weechat.color.chat_nick_self"
                user.nick_color = "weechat.color.chat_nick_self"

            self.weechat_buffer.join(
                buffer_user,
                server_ts_to_weechat(event.timestamp),
                not is_state
            )

        date = server_ts_to_weechat(event.timestamp)

        if isinstance(event, RoomMemberJoin):
            if event.prev_content and "membership" in event.prev_content:
                if (event.prev_content["membership"] == "leave"
                        or event.prev_content["membership"] == "invite"):
                    join(event, date, is_state)
                else:
                    # TODO print out profile changes
                    return
            else:
                # No previous content for this user in this room, so he just
                # joined.
                join(event, date, is_state)

        elif isinstance(event, RoomMemberLeave):
            # TODO the nick can be a display name or a full sender name
            nick = shorten_sender(event.sender)
            if event.sender == event.leaving_user:
                self.weechat_buffer.part(nick, date, not is_state)
            else:
                self.weechat_buffer.kick(nick, date, not is_state)

        elif isinstance(event, RoomMemberInvite):
            if is_state:
                return

            self.weechat_buffer.invite(event.invited_user, date)
            return

        room_name = self.room.display_name(self.room.own_user_id)
        self.weechat_buffer.short_name = room_name

    def _redact_line(self, event):
        def predicate(event_id, line):
            def already_redacted(tags):
                if SCRIPT_NAME + "_redacted" in tags:
                    return True
                return False

            event_tag = SCRIPT_NAME + "_id_{}".format(event_id)
            tags = line.tags

            if event_tag in tags and not already_redacted(tags):
                return True

            return False

        lines = self.weechat_buffer.find_lines(
            partial(predicate, event.redaction_id)
        )

        # No line to redact, return early
        if not lines:
            return

        # TODO multiple lines can contain a single matrix ID, we need to redact
        # them all
        line = lines[0]

        # TODO the censor may not be in the room anymore
        censor = self.room.users[event.sender].name
        message = line.message
        tags = line.tags

        reason = ("" if not event.reason else
                  ", reason: \"{reason}\"".format(reason=event.reason))

        redaction_msg = ("{del_color}<{log_color}Message redacted by: "
                         "{censor}{log_color}{reason}{del_color}>"
                         "{ncolor}").format(
                             del_color=W.color("chat_delimiters"),
                             ncolor=W.color("reset"),
                             log_color=W.color("logger.color.backlog_line"),
                             censor=censor,
                             reason=reason)

        new_message = ""

        if OPTIONS.redaction_type == RedactType.STRIKETHROUGH:
            plaintext_msg = W.string_remove_color(message, '')
            new_message = string_strikethrough(plaintext_msg)
        elif OPTIONS.redaction_type == RedactType.NOTICE:
            new_message = message
        elif OPTIONS.redaction_type == RedactType.DELETE:
            pass

        message = " ".join(s for s in [new_message, redaction_msg] if s)

        tags.append("matrix_redacted")

        line.message = message
        line.tags = tags

    def _handle_redacted_message(self, event):
        # TODO user doesn't have to be in the room anymore
        user = self.room.users[event.sender]
        date = server_ts_to_weechat(event.timestamp)
        tags = self.get_event_tags(event)
        tags.append(SCRIPT_NAME + "_redacted")

        reason = (", reason: \"{reason}\"".format(reason=event.reason)
                  if event.reason else "")

        censor = self.room.users[event.censor]

        data = ("{del_color}<{log_color}Message redacted by: "
                "{censor}{log_color}{reason}{del_color}>{ncolor}").format(
                   del_color=W.color("chat_delimiters"),
                   ncolor=W.color("reset"),
                   log_color=W.color("logger.color.backlog_line"),
                   censor=censor.name,
                   reason=reason)

        self.weechat_buffer.message(user.name, data, date, tags)

    def _handle_topic(self, event, is_state):
        try:
            user = self.room.users[event.sender]
            nick = user.name
        except KeyError:
            nick = event.sender

        self.weechat_buffer.change_topic(
            nick,
            event.topic,
            server_ts_to_weechat(event.timestamp),
            not is_state)

    @staticmethod
    def get_event_tags(event):
        return ["matrix_id_{}".format(event.event_id)]

    def handle_state_event(self, event):
        if isinstance(event, RoomMembershipEvent):
            self.handle_membership_events(event, True)
        elif isinstance(event, RoomTopicEvent):
            self._handle_topic(event, True)

    def handle_timeline_event(self, event):
        if isinstance(event, RoomMembershipEvent):
            self.handle_membership_events(event, False)
        elif isinstance(event, (RoomNameEvent, RoomAliasEvent)):
            room_name = self.room.display_name(self.room.own_user_id)
            self.weechat_buffer.short_name = room_name
        elif isinstance(event, RoomTopicEvent):
            self._handle_topic(event, False)
        elif isinstance(event, RoomMessageText):
            user = self.room.users[event.sender]
            data = (event.formatted_message.to_weechat()
                    if event.formatted_message else event.message)

            date = server_ts_to_weechat(event.timestamp)
            self.weechat_buffer.message(
                user.name,
                data,
                date,
                self.get_event_tags(event)
            )
        elif isinstance(event, RoomMessageEmote):
            user = self.room.users[event.sender]
            date = server_ts_to_weechat(event.timestamp)
            self.weechat_buffer.action(
                user.name,
                event.message,
                date,
                self.get_event_tags(event)
            )
        elif isinstance(event, RoomRedactionEvent):
            self._redact_line(event)
        elif isinstance(event, RoomRedactedMessageEvent):
            self._handle_redacted_message(event)

    def self_message(self, message):
        user = self.room.users[self.room.own_user_id]
        data = (message.formatted_message.to_weechat()
                if message.formatted_message
                else message.message)

        date = server_ts_to_weechat(message.timestamp)
        self.weechat_buffer.self_message(user.name, data, date)

    def self_action(self, message):
        user = self.room.users[self.room.own_user_id]
        date = server_ts_to_weechat(message.timestamp)
        self.weechat_buffer.self_action(user.name, message.message, date)
