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
import datetime

from tornado import gen
from tornado.options import define, options, parse_command_line
import tweetstream

define("port", default=8888, help="run on the given port", type=int)

logging.getLogger('tornado.access').setLevel(logging.CRITICAL)

def link_it_up(tweet):
    html = tweet['text']
    replacements = []
    for entity_type, entities in tweet['entities'].iteritems():
        if entity_type in ['hashtags', 'user_mentions', 'urls', 'media']:
            for entity in entities:
                generic_entity = {
                    'indices': entity['indices']
                }
                url = '#'
                title = ''
                if entity_type == 'hashtags':
                    url = 'https://twitter.com/search?q=%23' + entity['text']
                elif entity_type == 'user_mentions':
                    url = 'https://twitter.com/' + entity['screen_name']
                elif entity_type == 'urls' or entity_type == 'media':
                    url = entity['url']
                    title = entity['expanded_url']
                    generic_entity['text'] = entity['display_url']

                generic_entity['before'] = '<a href="' + url + '" target="_blank" title="' + title + '">'
                generic_entity['after'] = '</a>'
                replacements.append(generic_entity)

    replacements.sort(key=lambda entity: entity['indices'][0])

    offset = 0
    for replacement in replacements:
        old_text = html[replacement['indices'][0] + offset:replacement['indices'][1] + offset]
        if 'text' in replacement:
            new_text = replacement['text']
        else:
            new_text = old_text
        html = html[:replacement['indices'][0] + offset] + \
               replacement['before'] + new_text + replacement['after'] + \
               html[replacement['indices'][1] + offset:]
        offset += len(replacement['before']) - len(old_text) + len(new_text) + len(replacement['after'])
    return html


def create_message(tweet):
    if 'user' not in tweet:
        logging.error("couldn't create message from tweet: " + tornado.escape.json_encode(tweet))
        return None

    if 'retweeted_status' in tweet:
        user = tweet['user']
        tweet = tweet['retweeted_status']
        tweet['retweeted_by'] = user

    tweet['html'] = link_it_up(tweet)
    message = {
        "id": str(uuid.uuid4()),
        "tweet": tweet,
    }
    return message

def tweetstream_callback(tweet, key):
    message = create_message(tweet)
    if message:
        unique_rooms = set()
        for hashtag_info in tweet['entities']['hashtags']:
            unique_rooms.add(hashtag_info['text'].lower())
        for room in unique_rooms:
            if key in message_buffers and room in message_buffers[key]:
                message_buffers[key][room].new_messages([message])

# key -> stream
streams = {}

# key -> rooms
rooms = defaultdict(list)


class MessageBuffer(object):
    def __init__(self):
        self.waiters = set()
        self.cache = []
        self.cache_size = 200
        self.timeouts = {}

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
        self.timeouts[callback] = tornado.ioloop.IOLoop.current().add_timeout(datetime.timedelta(seconds=20), lambda: self.new_messages([]))
        self.waiters.add(callback)

    def cancel_timeout(self, callback):
        timeout = self.timeouts[callback]
        tornado.ioloop.IOLoop.current().remove_timeout(timeout)
        del self.timeouts[callback]

    def cancel_wait(self, callback):
        self.cancel_timeout(callback)
        self.waiters.remove(callback)

    def new_messages(self, messages):
        # logging.info("Sending new message to %r listeners", len(self.waiters))
        for callback in self.waiters:
            self.cancel_timeout(callback)
            try:
                callback(messages)
            except:
                logging.error("Error in waiter callback", exc_info=True)
        self.waiters = set()
        self.cache.extend(messages)
        if len(self.cache) > self.cache_size:
            self.cache = self.cache[-self.cache_size:]


# key -> room -> MessageBuffer
message_buffers = defaultdict(lambda: defaultdict(MessageBuffer))

class BaseHandler(tornado.web.RequestHandler):
    def get_current_user(self):
        user_json = self.get_secure_cookie("chatdemo_user")
        if not user_json: return None
        return tornado.escape.json_decode(user_json)

class StartStreamMixin(tornado.auth.TwitterMixin):
    @tornado.gen.coroutine
    def start_stream(self, room, screen_name=None, key=None, secret=None):
        if screen_name is None:
            key = os.environ["TWITTER_ACCESS_TOKEN"]
            secret = os.environ["TWITTER_ACCESS_TOKEN_SECRET"]

        if room in rooms[key]:
            return

        rooms[key].append(room)

        if screen_name:
            logging.info('@' + screen_name + ' joined #' + room)
        else:
            logging.info('someone' + ' joined #' + room)

        if key in streams:
            streams[key].close()


        results = yield self.twitter_request(
            '/search/tweets', access_token={'key': key, 'secret': secret},
            q='#' + room, result_type='recent', count=100
        )

        if results:
            messages = []
            for tweet in results['statuses']:
                message = create_message(tweet)
                if message:
                    messages.append(message)
            messages.reverse()
            message_buffers[key][room].new_messages(messages)

        configuration = {
            "twitter_consumer_key": os.environ["TWITTER_CONSUMER_KEY"],
            "twitter_consumer_secret": os.environ["TWITTER_CONSUMER_SECRET"],
            "twitter_access_token": key,
            "twitter_access_token_secret": secret,
        }

        streams[key] = tweetstream.TweetStream(configuration)
        streams[key].fetch("/1.1/statuses/filter.json?" + urllib.urlencode({'track': ','.join(['#' + room for room in rooms[key]])}), callback=lambda tweet: tweetstream_callback(tweet, key))


class MainHandler(BaseHandler):
    def get(self):
        self.render("index.html")


class MessageNewHandler(BaseHandler, tornado.auth.TwitterMixin):
    @tornado.web.authenticated
    @tornado.web.asynchronous
    @tornado.gen.coroutine
    def post(self):
        result = yield self.twitter_request(
            '/statuses/update', access_token=self.current_user["access_token"], 
            post_args={
                'status': tornado.escape.utf8(self.get_argument("body")),
                'in_reply_to_status_id': self.get_argument('in_reply_to')
            }
        )
        if self.get_argument("next", None):
            self.redirect(self.get_argument("next"))
        else:
            self.write(result)
        # global_message_buffer.new_messages([message])


class MessageUpdatesHandler(BaseHandler, StartStreamMixin):
    @tornado.web.asynchronous
    def post(self, room):
        self.room = room
        cursor = self.get_argument("cursor", None)
        user = self.get_current_user()
        if user:
            self.key = user['access_token']['key']
            self.start_stream(room, user['screen_name'], user['access_token']['key'], user['access_token']['secret'])
        else:
            self.key = os.environ['TWITTER_ACCESS_TOKEN']
            self.start_stream(room)
        message_buffers[self.key][self.room].wait_for_messages(self.on_new_messages,
                                                cursor=cursor)

    def on_new_messages(self, messages):
        # Closed client connection
        if self.request.connection.stream.closed():
            return
        for message in messages:
            if 'html' not in message:
                message['html'] = tornado.escape.to_basestring(
                                        self.render_string("tweet.html", tweet=message['tweet'], room=self.room))
        self.finish(dict(messages=messages))

    def on_connection_close(self):
        message_buffers[self.key][self.room].cancel_wait(self.on_new_messages)


class AuthLoginHandler(BaseHandler, tornado.auth.TwitterMixin):
    @tornado.web.asynchronous
    def get(self):
        if self.get_argument("oauth_token", None):
            self.get_authenticated_user(self.async_callback(self._on_auth))
            return
        if self.get_argument("denied", None):
            # TODO: need to change if we ever have pages that absolutely require authentication
            self.redirect(self.get_argument('next'))
            return
        self.authorize_redirect('/auth/login?next=' + self.get_argument('next'))
    def _on_auth(self, user):
        user_data = {
            'screen_name': user['screen_name'],
            'access_token': user['access_token'],
            'profile_image_url': user['profile_image_url'],
        }
        self.set_secure_cookie("chatdemo_user",
                               tornado.escape.json_encode(user_data))
        self.redirect(self.get_argument('next'))


class AuthLogoutHandler(BaseHandler):
    def get(self):
        self.clear_cookie("chatdemo_user")
        self.redirect('/')
        # self.write("You are now logged out")


class RoomsHandler(BaseHandler, StartStreamMixin):
    def initialize(self, room=None):
        self.room = room
    def post(self):
        room = self.get_argument("room", None)
        if room is not None and len(room) > 0:
            if room[0] == '#':
                room = room[1:]
            self.redirect("/rooms/" + room.lower())
        else:
            self.redirect("/")

    @tornado.web.asynchronous
    @tornado.gen.coroutine
    def get(self, room=None):
        self.set_header('Cache-Control', 'no-cache, no-store, must-revalidate')

        if room is None:
            room = self.room
        if room != room.lower():
            self.redirect("/rooms/" + room.lower())
            return
        user = self.get_current_user()
        if user:
            screen_name = user['screen_name']
            key = user['access_token']['key']
            secret = user['access_token']['secret']
            yield self.start_stream(room, screen_name, key, secret)
        else:
            login_url = '/auth/login?next=' + self.request.uri
            key = os.environ["TWITTER_ACCESS_TOKEN"]
            if len(rooms[key]) > 390:
                self.redirect(login_url)
                return

            screen_name = None
            try:
                yield self.start_stream(room)
            except:
                self.redirect(login_url)
                return

        self.render("room.html", room=room, messages=message_buffers[key][room].cache)


class AdminHandler(BaseHandler):
    def get(self):
        key = os.environ["TWITTER_ACCESS_TOKEN"]
        self.write(str(len(rooms[key])))


class AdminMemoryHandler(BaseHandler):
    def get(self):
        from sys import getsizeof
        self.write("storing messages for " + str(len(message_buffers)) + " users<br>")
        total = 0
        size_total = 0
        user_room_list = []
        for user_key, user_message_buffers in message_buffers.iteritems():
            total += len(user_message_buffers)
            for room_key, message_buffer in user_message_buffers.iteritems():
                size_total += getsizeof(message_buffer)
                user_room_list.append((user_key, room_key))
        self.write("total message buffers: " + str(total) + "<br>")

        for user, room in user_room_list:
            self.write(user.split('-')[0] + " " + room + "<br>")


def main():
    parse_command_line()
    handlers = [
        (r"/auth/login", AuthLoginHandler),
        (r"/auth/logout", AuthLogoutHandler),
        (r"/a/message/new", MessageNewHandler),
        (r"/a/message/updates/([a-z0-9_]+)", MessageUpdatesHandler),
        (r"/admin", AdminHandler),
        (r"/admin/memory", AdminMemoryHandler),
    ]

    room = os.environ.get('ROOM')
    if room:
        handlers.extend([
            (r"/", RoomsHandler, {"room": room}),
        ])
    else:
        handlers.extend([
            (r"/", MainHandler),
            (r"/rooms", RoomsHandler),
            (r"/rooms/([a-zA-Z0-9_]+)", RoomsHandler),
        ])

    app = tornado.web.Application(handlers,
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
