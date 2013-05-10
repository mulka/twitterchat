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

from tornado import gen
from tornado.options import define, options, parse_command_line
import tweetstream

define("port", default=8888, help="run on the given port", type=int)


def tweetstream_callback(tweet):
    if 'user' in tweet:
        message = {
            "id": str(uuid.uuid4()),
            "from": tweet['user']['screen_name'],
            "body": tweet['text'],
            "profile_image_url": tweet['user']['profile_image_url']
        }

        global_message_buffer.new_messages([message])

stream_started = False

def start_stream(key, secret, room='mothersday'):
    global stream_started

    if stream_started:
        return
    stream_started = True
    configuration = {
        "twitter_consumer_key": os.environ["TWITTER_CONSUMER_KEY"],
        "twitter_consumer_secret": os.environ["TWITTER_CONSUMER_SECRET"],
        "twitter_access_token": key,
        "twitter_access_token_secret": secret,
    }

    stream = tweetstream.TweetStream(configuration)
    stream.fetch("/1.1/statuses/filter.json?" + urllib.urlencode({'track': '#' + room}), callback=tweetstream_callback)


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
        logging.info("Sending new message to %r listeners", len(self.waiters))
        for callback in self.waiters:
            try:
                callback(messages)
            except:
                logging.error("Error in waiter callback", exc_info=True)
        self.waiters = set()
        self.cache.extend(messages)
        if len(self.cache) > self.cache_size:
            self.cache = self.cache[-self.cache_size:]


# Making this a non-singleton is left as an exercise for the reader.
global_message_buffer = MessageBuffer()


class BaseHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        user_json = self.get_secure_cookie("chatdemo_user")
        if not user_json: return None
        return tornado.escape.json_decode(user_json)


class MainHandler(BaseHandler):
    @tornado.web.authenticated
    def get(self):
        self.render("index.html", messages=global_message_buffer.cache)


class MessageNewHandler(BaseHandler):
    @tornado.web.authenticated
    def post(self):
        message = {
            "id": str(uuid.uuid4()),
            "from": self.current_user["username"],
            "body": self.get_argument("body"),
        }
        # to_basestring is necessary for Python 3's json encoder,
        # which doesn't accept byte strings.
        message["html"] = tornado.escape.to_basestring(
            self.render_string("message.html", message=message))
        if self.get_argument("next", None):
            self.redirect(self.get_argument("next"))
        else:
            self.write(message)
        global_message_buffer.new_messages([message])


class MessageUpdatesHandler(BaseHandler):
    @tornado.web.authenticated
    @tornado.web.asynchronous
    def post(self):
        cursor = self.get_argument("cursor", None)
        user = self.get_current_user()
        start_stream(user['access_token']['key'], user['access_token']['secret'])
        global_message_buffer.wait_for_messages(self.on_new_messages,
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
        global_message_buffer.cancel_wait(self.on_new_messages)


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


def main():
    parse_command_line()
    app = tornado.web.Application(
        [
            (r"/", MainHandler),
            (r"/auth/login", AuthLoginHandler),
            (r"/auth/logout", AuthLogoutHandler),
            (r"/a/message/new", MessageNewHandler),
            (r"/a/message/updates", MessageUpdatesHandler),
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
