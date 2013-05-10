#!/usr/bin/env python
#
# Copyright 2009 Facebook
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may
# not use this file except in compliance with the License. You may obtain
# a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.

import logging
import tornado.auth
import tornado.escape
import tornado.ioloop
import tornado.web
import os
import os.path
import urllib
import uuid
from collections import defaultdict

from tornado import gen
from tornado.options import define, options, parse_command_line
import tweetstream

define("port", default=8888, help="run on the given port", type=int)

logging.getLogger('tornado.access').setLevel(logging.CRITICAL)

def tweetstream_callback(tweet, screen_name):
    if 'user' in tweet:
        message = {
            "id": str(uuid.uuid4()),
            "from": tweet['user']['screen_name'],
            "body": tweet['text'],
            "profile_image_url": tweet['user']['profile_image_url']
        }

        for hashtag_info in tweet['entities']['hashtags']:
            room = hashtag_info['text'].lower()
            if screen_name in message_buffers and room in message_buffers[screen_name]:
                message_buffers[screen_name][room].new_messages([message])

# screen_name -> stream
streams = {}

# screen_name -> rooms
rooms = defaultdict(list)

def start_stream(screen_name, key, secret, room):
    global streams

    if room in rooms[screen_name]:
        return

    rooms[screen_name].append(room)

    logging.info('@' + screen_name + ' joined #' + room)
    if screen_name in streams:
        streams[screen_name].close()

    configuration = {
        "twitter_consumer_key": os.environ["TWITTER_CONSUMER_KEY"],
        "twitter_consumer_secret": os.environ["TWITTER_CONSUMER_SECRET"],
        "twitter_access_token": key,
        "twitter_access_token_secret": secret,
    }

    streams[screen_name] = tweetstream.TweetStream(configuration)
    streams[screen_name].fetch("/1.1/statuses/filter.json?" + urllib.urlencode({'track': ','.join(['#' + room for room in rooms[screen_name]])}), callback=lambda tweet: tweetstream_callback(tweet, screen_name))


class MessageBuffer(object):
    def __init__(self):
        self.waiters = set()
        self.cache = []
        self.cache_size = 200

    def wait_for_messages(self, callback, cursor=None):
        if cursor:
            new_count = 0
            for msg in reversed(self.cache):
                if msg["id"] == cursor:
                    break
                new_count += 1
            if new_count:
                callback(self.cache[-new_count:])
                return
        self.waiters.add(callback)

    def cancel_wait(self, callback):
        self.waiters.remove(callback)

    def new_messages(self, messages):
        # logging.info("Sending new message to %r listeners", len(self.waiters))
        for callback in self.waiters:
            try:
                callback(messages)
            except:
                logging.error("Error in waiter callback", exc_info=True)
        self.waiters = set()
        self.cache.extend(messages)
        if len(self.cache) > self.cache_size:
            self.cache = self.cache[-self.cache_size:]


# screen_name -> room -> MessageBuffer
message_buffers = defaultdict(lambda: defaultdict(MessageBuffer))

class BaseHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        user_json = self.get_secure_cookie("chatdemo_user")
        if not user_json: return None
        return tornado.escape.json_decode(user_json)


class MainHandler(BaseHandler):
    @tornado.web.authenticated
    def get(self):
        self.render("index.html")


class MessageNewHandler(BaseHandler):
    @tornado.web.authenticated
    def post(self):
        pass
        # TODO: change this to post to Twitter instead of putting it directly into the message buffer
        # message = {
        #     "id": str(uuid.uuid4()),
        #     "from": self.current_user["username"],
        #     "body": self.get_argument("body"),
        # }
        # # to_basestring is necessary for Python 3's json encoder,
        # # which doesn't accept byte strings.
        # message["html"] = tornado.escape.to_basestring(
        #     self.render_string("message.html", message=message))
        # if self.get_argument("next", None):
        #     self.redirect(self.get_argument("next"))
        # else:
        #     self.write(message)
        # # global_message_buffer.new_messages([message])


class MessageUpdatesHandler(BaseHandler):
    @tornado.web.authenticated
    @tornado.web.asynchronous
    def post(self, room):
        self.room = room
        cursor = self.get_argument("cursor", None)
        user = self.get_current_user()
        self.screen_name = user['screen_name']
        start_stream(self.screen_name, user['access_token']['key'], user['access_token']['secret'], room)
        message_buffers[self.screen_name][self.room].wait_for_messages(self.on_new_messages,
                                                cursor=cursor)

    def on_new_messages(self, messages):
        # Closed client connection
        if self.request.connection.stream.closed():
            return
        for message in messages:
            if 'html' not in message:
                message['html'] = tornado.escape.to_basestring(
                                        self.render_string("message.html", message=message))
        self.finish(dict(messages=messages))

    def on_connection_close(self):
        message_buffers[self.screen_name][self.room].cancel_wait(self.on_new_messages)


class AuthLoginHandler(BaseHandler, tornado.auth.TwitterMixin):
    @tornado.web.asynchronous
    def get(self):
        if self.get_argument("oauth_token", None):
            self.get_authenticated_user(self.async_callback(self._on_auth))
            return
        self.authorize_redirect('/auth/login')
    def _on_auth(self, user):
        self.set_secure_cookie("chatdemo_user",
                               tornado.escape.json_encode(user))
        self.redirect("/")


class AuthLogoutHandler(BaseHandler):
    def get(self):
        self.clear_cookie("chatdemo_user")
        self.write("You are now logged out")


class RoomsHandler(BaseHandler):
    def post(self):
        room = self.get_argument("room", None)
        if room is not None:
            self.redirect("/rooms/" + room.lower())
        else:
            self.redirect("/")
    @tornado.web.authenticated
    def get(self, room):
        user = self.get_current_user()
        screen_name = user['screen_name']
        self.render("room.html", room=room, messages=message_buffers[screen_name][room].cache)


def main():
    parse_command_line()
    app = tornado.web.Application(
        [
            (r"/", MainHandler),
            (r"/auth/login", AuthLoginHandler),
            (r"/auth/logout", AuthLogoutHandler),
            (r"/a/message/new", MessageNewHandler),
            (r"/a/message/updates/([a-z0-9_]+)", MessageUpdatesHandler),
            (r"/rooms", RoomsHandler),
            (r"/rooms/([a-z0-9_]+)", RoomsHandler),
            ],
        cookie_secret=os.environ["COOKIE_SECRET"],
        login_url="/auth/login",
        template_path=os.path.join(os.path.dirname(__file__), "templates"),
        static_path=os.path.join(os.path.dirname(__file__), "static"),
        xsrf_cookies=True,
        twitter_consumer_key=os.environ["TWITTER_CONSUMER_KEY"],
        twitter_consumer_secret=os.environ["TWITTER_CONSUMER_SECRET"],
        debug=(os.environ.get('DEBUG', 'false') == 'true'),
        )
    app.listen(options.port)
    tornado.ioloop.IOLoop.instance().start()


if __name__ == "__main__":
    main()
