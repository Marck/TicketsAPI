import json
import time
import os
import markdown
import html
import jinja2 as jinja2_original

from sanic import response, Sanic
from datetime import timedelta
from sanic_jinja2 import SanicJinja2
from sanic_scheduler import SanicScheduler, task
from utils import sqlite, tickets, jinja_filters

# Sanic itself
app = Sanic()
app.static('/static', './static')

# Sanic plugins
scheduler = SanicScheduler(app)
jinja = SanicJinja2(app)

# General configs
db = sqlite.Database()
db.create_tables()  # Attempt to create table(s) if not exists already.
config = json.load(open("config.json", "r"))
md = markdown.Markdown(extensions=["meta"])

# Jinja2 template filters
jinja.env.filters["markdown"] = lambda text: jinja2_original.Markup(md.convert(text))
jinja.env.filters["discord_to_html"] = lambda text: jinja_filters.discord_to_html(text)
jinja.env.filters["find_url"] = lambda text: jinja_filters.match_url(text)
jinja.env.filters["detect_file"] = lambda file: jinja_filters.detect_file(file)


@app.route("/")
@jinja.template("index.html")
async def index(request):
    return {"test": "This is a nice test"}


@task(timedelta(seconds=60))
def delete_old_tickets(app):
    """ Delete old ticket entries for privacy reasons """
    db.execute("DELETE FROM tickets WHERE ? > expire", (int(time.time()),))


@app.route("/<ticket_id:string>")
@jinja.template("ticket.html")
async def show_ticket(request, ticket_id):
    ticket_db = tickets.Ticket(db=db)
    data = ticket_db.fetch_ticket(ticket_id)

    if not data:
        return {"status": 404, "code": ticket_id}

    if str(data["submitted_by"]) == str(config["bot_id"]):
        valid_source = tickets.TicketSource.valid
    else:
        valid_source = tickets.TicketSource.unknown

    get_logs = json.loads(data["logs"])

    # To prevent XSS, fucking hell it's shit, but I'll find a better solution later...
    converted_logs = []
    for msg in get_logs["messages"]:
        temp_holder = []
        for content in msg["content"]:
            if content["msg"]:
                converted_msg = html.escape(content["msg"]).encode("ascii", "xmlcharrefreplace").decode()
            else:
                converted_msg = None

            temp_holder.append({
                "msg": converted_msg,
                "attachments": content["attachments"] if "attachments" in content else False,
                "edited": content["edited"] if "edited" in content and content["edited"] else False,
                "deleted": True if "deleted" in content and content["deleted"] else False
            })

        converted_logs.append({
            "author": msg["author"], "timestamp": msg["timestamp"],
            "content": temp_holder
        })

    get_logs["messages"] = converted_logs

    return {
        "status": 200, "title": f"#{get_logs['channel_name']} | xelA Tickets", "submitted_by": data["submitted_by"],
        "ticket_id": data["ticket_id"], "guild_id": data["guild_id"], "author_id": str(data["author_id"]),
        "created_at": data["created_at"], "confirmed_by": str(data["confirmed_by"]),
        "expires": data["expire"], "context": data["context"], "official_bot": config["bot_id"],
        "channel_name": get_logs["channel_name"], "valid_source": valid_source, "logs": get_logs
    }


@app.route("/<ticket_id:string>/download")
async def download_ticket(request, ticket_id):
    ticket_db = tickets.Ticket(db=db)
    data = ticket_db.fetch_ticket(ticket_id)

    if not data:
        return response.json({"status": 404, "code": ticket_id}, status=404)

    return response.json(json.loads(data["logs"]))


@app.route("/submit/example")
async def submit_example(request):
    with open("examples/submit.json", "r") as f:
        data = json.load(f)

    return response.json(data)


@app.route("/submit", methods=["POST"])
async def submit(request):
    token = request.headers.get("Authorization") or None
    uploaded_file = False

    if request.files:
        uploaded_file = True
        file = request.files.get("ticket_file") or None
        if not file:
            return response.json({"status": 400, "message": "Unknown filename..."}, status=400)
        if file.type != "application/json":
            return response.json({"status": 400, "message": "Invalid filetype... (Only accepts JSON)"}, status=400)

        try:
            post_data = json.loads(file.body)
        except Exception:
            return response.json({"status": 400, "message": "Somehow, this JSON is broken..."}, status=400)
    else:
        if not request.json:
            return response.json({"status": 400, "message": "Missing JSON/data"}, status=400)
        post_data = request.json

    if "submitted_by" not in post_data:
        return response.json({"status": 400, "message": "Missing 'submitted_by' in JSON"}, status=400)

    if post_data["submitted_by"] == config["bot_id"]:
        if not token:
            post_data["submitted_by"] = "86477779717066752"  # If a user is uploading the JSON file without changing submitted_by
        if token and token != config["token"]:
            return response.json({"status": 403, "message": "Invalid Authorization token..."}, status=403)

    make_ticket = tickets.Ticket(payload=post_data, db=db)
    code, data = make_ticket.attempt_post()

    if code != 200:
        return response.json({"status": code, "message": data.message, "validator": data.validator}, status=code)

    if uploaded_file:
        return response.redirect(f"/{data}")
    else:
        return response.json({"status": code, "message": data})


if __name__ == "__main__":
    # Sanic + Windows ports is weird so you can only have 1 worker on it...
    workers = 1 if os.name == "nt" else config["sanic_workers"]
    app.run(host="0.0.0.0", port=config["port"], workers=workers)
