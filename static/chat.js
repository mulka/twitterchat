$(document).ready(function() {
    if (!window.console) window.console = {};
    if (!window.console.log) window.console.log = function() {};
    $(document).on("submit", "#chat-form", function() {
        newMessage($(this));
        return false;
    });
    $(document).on("keydown", "#chat-form", function(e) {
        if (e.keyCode == 13) {
            newMessage($(this));
            return false;
        }
    });
    $("#message").select();
    updater.poll();
});

var tweets = {};

function reply(tweet_id) {
    var form = $("#chat-form");
    $("#in_reply_to").val(tweet_id);
    var el = form.find("textarea");
    el.val('@' + tweets[tweet_id]['user']['screen_name'] + ' ' + el.val()).select();
    return false;
}

function quote(tweet_id) {
    var el = $("#chat-form").find("textarea");
    var t = tweets[tweet_id];
    el.val(el.val() + ' "@' + t['user']['screen_name'] + ': ' + t['text'] + '"').select();
    return false;
}

function newMessage(form) {
    var message = form.formToDict();
    message.body += ' #' + room;
    var disabled = form.find("input[type=submit]");
    disabled.disable();
    $.postJSON("/a/message/new", message, function(response) {
        form.find("textarea").val("").select();
        disabled.enable();
    });
}

function getCookie(name) {
    var r = document.cookie.match("\\b" + name + "=([^;]*)\\b");
    return r ? r[1] : undefined;
}

jQuery.postJSON = function(url, args, callback) {
    args._xsrf = getCookie("_xsrf");
    $.ajax({url: url, data: $.param(args), dataType: "text", type: "POST",
            success: function(response) {
        if (callback) callback(eval("(" + response + ")"));
    }, error: function(response) {
        console.log("ERROR:", response)
    }});
};

jQuery.fn.formToDict = function() {
    var fields = this.serializeArray();
    var json = {}
    for (var i = 0; i < fields.length; i++) {
        json[fields[i].name] = fields[i].value;
    }
    if (json.next) delete json.next;
    return json;
};

jQuery.fn.disable = function() {
    this.enable(false);
    return this;
};

jQuery.fn.enable = function(opt_enable) {
    if (arguments.length && !opt_enable) {
        this.attr("disabled", "disabled");
    } else {
        this.removeAttr("disabled");
    }
    return this;
};

var updater = {
    errorSleepTime: 500,
    cursor: null,

    poll: function() {
        var args = {"_xsrf": getCookie("_xsrf")};
        if (updater.cursor) args.cursor = updater.cursor;
        $.ajax({url: "/a/message/updates/" + room, type: "POST", dataType: "text",
                data: $.param(args), success: updater.onSuccess,
                error: updater.onError});
    },

    onSuccess: function(response) {
        try {
            updater.newMessages(eval("(" + response + ")"));
        } catch (e) {
            updater.onError();
            return;
        }
        updater.errorSleepTime = 500;
        window.setTimeout(updater.poll, 5000);
    },

    onError: function(response) {
        updater.errorSleepTime *= 2;
        console.log("Poll error; sleeping for", updater.errorSleepTime, "ms");
        window.setTimeout(updater.poll, updater.errorSleepTime);
    },

    newMessages: function(response) {
        if (!response.messages || !response.messages.length) return;
        updater.cursor = response.cursor;
        var messages = response.messages;
        updater.cursor = messages[messages.length - 1].id;
        console.log(messages.length, "new messages, cursor:", updater.cursor);
        for (var i = 0; i < messages.length; i++) {
            updater.showMessage(messages[i]);
        }
    },

    showMessage: function(message) {
        var existing = $("#m" + message.id);
        if (existing.length > 0) return;
        tweets[message.tweet.id_str] = message.tweet;
        var node = $(message.html);
        node.hide();
        $("#feed").prepend(node);
        node.slideDown();
    }
};
