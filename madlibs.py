"""

Twilio mad libs game

TODO:

  views:
  - libs should have twispy overlays or some way to simply view both result and prompt
  - buttons for all registered users, pagified for over X amount, different colors/disabled if not participate or already active somewhere

"""

from collections import namedtuple
import datetime
from email.mime.text import MIMEText
import hashlib
import random
import smtplib
import sqlite3
from threading import Timer

from twilio.rest import TwilioRestClient
from twilio import twiml

from flask import Flask, redirect, request, render_template, url_for

# App configuration
app = Flask(__name__)
app.config.from_pyfile('config.py')

SALT = 'f4w3nm3o-amc'

# Twilio client configuration
ACCOUNT_SID = "ACd95d96f963b645198712952ce845a0e8"
AUTH_TOKEN = "c728dcad98cfce0ec149881a7d63f5de"

CLIENT = TwilioRestClient(app.config['TWILIO_ACCOUNT_SID'], app.config['TWILIO_AUTH_TOKEN'])

# Database schema for sqlite3
Lib = namedtuple('Lib', ['id', 'recipe', 'author', 'datetime', 'status'])
Entry = namedtuple('Entry', ['id', 'libid', 'position', 'prompt', 'assignee', 'result'])
# TODO: Status? RowID?
# status codes: 0 - finished, 1 - active
User = namedtuple('User', ['name', 'phone', 'current_entry'])

def initialize_testdb(conn, db):
  db.execute('pragma foreign_keys = ON')
  db.execute('drop table if exists users')
  db.execute('drop table if exists entries')
  db.execute('drop table if exists libs')
  db.execute('create table libs (id integer primary key, recipe text, author text, datetime text, status integer)')
  db.execute('create table entries (id integer primary key, libid integer, position integer, prompt text, assignee text, result text, FOREIGN KEY(libid) REFERENCES libs(id))')
  db.execute('create table users (name text unique primary key, phone text unique, current_entry integer, FOREIGN KEY(current_entry) REFERENCES entries(id))')
  #db.execute('insert into users values ("charlie", "3109132832", null)')
  db.execute('insert into libs values (null, "hello {name}, you are really {adjective}", "charlie", datetime("now", "localtime"), 0)')
  #db.execute('insert into libs values (null, "charlie likes to {verb}", "charlie", strftime("%s", "now"), 0)')
  #db.execute('insert into libs values (null, "twilio is really {adjective}", "charlie", strftime("%s", "now"), 0)')
  conn.commit()


class LibValidationException(Exception):
  pass

#####
# Views
#####
@app.route('/', methods=['GET', 'POST'])
def home():
  error_msg = None
  success_msg = None
  if request.method == 'POST':
    try:
      new_rowid = add_new_lib(request.form)
      if new_rowid:
        send_field_reqs(new_rowid)
      success_msg = "Lib #%s posted! Now awaiting text replies from participants..." % str(new_rowid)
    except LibValidationException as e:
      error_msg = str(e)
      pass

  conn = sqlite3.connect(app.config['DB_NAME'])
  cursor = conn.cursor()
  cursor.execute('SELECT id, recipe, author, datetime, status FROM libs WHERE status = 0 ORDER BY datetime DESC LIMIT 10')

  past_libs = map(Lib._make, cursor.fetchall())

  cursor.execute('SELECT name, phone, current_entry FROM users ORDER BY name ASC')
  users = map(User._make, cursor.fetchall())

  lib_info = get_lib_entry_info([lib.id for lib in past_libs])

  cursor.close()
  conn.close()
  return render_template('index.html', users=users, past_libs=past_libs, lib_info=lib_info, error_msg=error_msg, success_msg=success_msg)

@app.route('/libs/<libid>')
def getlib(libid):
  conn = sqlite3.connect(app.config['DB_NAME'])
  cursor = conn.cursor()
  cursor.execute('SELECT id, recipe, author, datetime, status FROM libs WHERE id = ?', libid)
  lib = Lib._make(cursor.fetchone())
  lib_info = get_lib_entry_info([lib.id])
  return render_template('lib.html', lib_info=lib_info, lib=lib)

#####
# Adding a new lib
#####
def add_new_lib(form):
  """
  Creates a new lib and its assigned entries in the database.
  """
  fields, count = parse_fields(form['lib_recipe'])
  if count + (20*len(fields)) > 460:
    raise LibValidationException('Lib rejected: Libs are limited to 460 characters, with fields counting as 20 each.')

  # Assign fields randomly to all participants. # participants must equal # fields.
  participants = form['participants'].split()
  random.shuffle(participants)
  if len(fields) != len(participants):
    raise LibValidationException('Lib rejected: The number of fields must be equal to the number of participants')

  conn = sqlite3.connect(app.config['DB_NAME'])
  cursor = conn.cursor()
  cursor.execute('pragma foreign_keys = ON')

  # Add the new Lib row.
  cursor.execute('insert into libs values (null, ?, ?, datetime("now", "localtime"), 1)', (form['lib_recipe'], form['author']))
  libid = cursor.lastrowid

  # Add those Entry rows, update the User rows who point to the inserted lib.
  for index, participant in enumerate(participants):
    cursor.execute('insert into entries values (null, ?, ?, ?, ?, "")', (libid, index, fields[index], participant))
    cursor.execute('update users set current_entry = ? where name = ?', (cursor.lastrowid, participant))

  conn.commit()
  cursor.close()
  conn.close()
  return libid
  # TODO: Kick off threading.Timer.start() with a timeout function.

#####
# Lib text parsing
#####
def parse_fields(recipe):
  """
  Given a lib recipe, return a sequence of the fields and the number of characters not in fields.
  Fields are defined as strings in between curly braces. {field}

  Also validates. Returns False if the recipe is not valid.
  """
  if recipe.count('{') != recipe.count('}'):
    return False

  count = 0
  fields = []
  while '{' in recipe and '}' in recipe:
    left_brace = recipe.find('{')
    right_brace = recipe.find('}')
    if right_brace < left_brace:
      return False
    count += left_brace
    fields.append(recipe[left_brace+1:right_brace])
    recipe = recipe[right_brace+1:]

  count += len(recipe)
  return fields, count

def get_final_string(libid):
  """
  Given a libid in the database, fill in all the fields with the results capitalized.
  """
  conn = sqlite3.connect(app.config['DB_NAME'])
  cursor = conn.cursor()
  cursor.execute('SELECT id, libid, position, prompt, assignee, result FROM entries WHERE libid = ? ORDER BY position ASC', (libid,))
  entries = map(Entry._make, cursor.fetchall())

  cursor.execute('SELECT recipe FROM libs WHERE id = ?', (libid,))
  recipe = cursor.fetchone()[0]

  print "Here is the recipe: %s" % recipe
  print entries

  # Create the final string! Got the recipe, got the entries in order.
  for entry in entries:
    field = '{' + entry.prompt + '}'
    print field
    recipe = recipe.replace(field, entry.result.upper(), 1)
    print "Replacing..."
    print recipe
    print type(recipe)

  cursor.close()
  conn.close()
  return recipe

def get_lib_entry_info(libs):
  """
  Given a sequence of libs, create a representation for the home page.
  """
  conn = sqlite3.connect(app.config['DB_NAME'])
  cursor = conn.cursor()
  result = {}
  for lib in libs:
    cursor.execute('SELECT id, libid, position, prompt, assignee, result FROM entries WHERE libid = ? ORDER BY position ASC', (lib,))
    entries = map(Entry._make, cursor.fetchall())

    cursor.execute('SELECT recipe FROM libs WHERE id = ?', (lib,))
    recipe = cursor.fetchone()[0]

    # Create the final string! Got the recipe, got the entries in order.
    for entry in entries:
      field = '{' + entry.prompt + '}'
      recipe = recipe.replace(field, '%s (%s, %s)' % (entry.result, entry.assignee, entry.prompt), 1)

    result[lib] = recipe

  cursor.close()
  conn.close()
  return result

#####
# Text message handling
#####
@app.route('/handletext', methods=['GET', 'POST'])
def handletext():
  """
  Upon receiving a text, find out who sent it, update the Entry and the User.

  If there are no more fields to fill, set the Lib status to finished (0) and send final texts.
  """
  conn = sqlite3.connect(app.config['DB_NAME'])
  cursor = conn.cursor()
  cursor.execute('SELECT name, phone, current_entry FROM users WHERE phone = ?', (request.args['From'],))
  user = User._make(cursor.fetchone())

  cursor.execute('UPDATE entries set result = ? where id = ?', (request.args['Body'][:20], user.current_entry))
  cursor.execute('UPDATE users set current_entry = null where phone = ?', (request.args['From'],))
  conn.commit()

  # Search all Entries for this lib. If none of the result strings are empty, the Lib is finished.
  cursor.execute('SELECT libid FROM entries WHERE id = ?', (user.current_entry,))
  libid = cursor.fetchone()[0]
  cursor.execute('SELECT result FROM entries WHERE libid = ?', (libid,))

  # If all of the results are not empty, update Lib and send final text.
  if all(map(lambda x: x[0] != '', cursor.fetchall())):
    cursor.execute('UPDATE libs set status = 0 where id = ?', (libid,))
    send_final_texts(libid)

  conn.commit()
  cursor.close()
  conn.close()

  return 'ack'


#####
# Text message broadcasting
#####
def send_field_reqs(libid):
  """
  Given a lib id, get all Entries that match that lib id and send texts to all of them.
  """
  conn = sqlite3.connect(app.config['DB_NAME'])
  cursor = conn.cursor()
  cursor.execute('SELECT id, libid, position, prompt, assignee, result FROM entries WHERE libid = ?', (libid,))

  entries = map(Entry._make, cursor.fetchall())

  for entry in entries:
    cursor.execute('SELECT name, phone, current_entry FROM users WHERE name = ?', (entry.assignee,))
    user = User._make(cursor.fetchone())
    text_body = 'Twilio Mad Libs! Please text me back (20 char max) with a: %s' % entry.prompt.upper()
    CLIENT.sms.messages.create(to=user.phone, from_=app.config['TWILIO_TXT_NUMBER'], body=text_body)

  cursor.close()
  conn.close()

def send_final_texts(libid):
  """
  Sends the final result string to all participants, split into 160 char chunks.
  """
  conn = sqlite3.connect(app.config['DB_NAME'])
  cursor = conn.cursor()

  result = 'Lib #%s: ' % str(libid) + get_final_string(libid)
  print result

  # Split the string into 160char chunks.
  messages = []
  while len(result) > 160:
    messages.append(result[:160])
    result = result[160:]
  messages.append(result)

  # Get recipients (fields and author)
  cursor.execute('SELECT phone FROM users, entries WHERE users.name = entries.assignee AND entries.libid = ?', (libid,))
  phone_numbers = map(lambda x: x[0], cursor.fetchall())
  cursor.execute('SELECT phone FROM users, libs WHERE users.name = libs.author AND libs.id = ?', (libid,))
  phone_numbers.append(cursor.fetchone()[0])
  phone_numbers = set(phone_numbers)

  for message in messages:
    for pn in phone_numbers:
      CLIENT.sms.messages.create(to=pn, from_=app.config['TWILIO_TXT_NUMBER'], body=message)

  cursor.close()
  conn.close()

#####
# Signup/unregister
#####
@app.route('/textregister', methods=['GET', 'POST'])
def handle_register_text():
  """
  Upon receiving a text at app.config['TWILIO_REG_NUMBER'], parse the body and either register or unregister an user.
  """
  if 'Body' not in request.args:
    return 'ack'

  text_body = request.args['Body']

  conn = sqlite3.connect(app.config['DB_NAME'])
  cursor = conn.cursor()
  twiml_response = twiml.Response()

  if text_body.lower().startswith('register'):
    user = text_body[len('register'):].strip().lower()
    try:
      cursor.execute('insert into users values (?, ?, null)', (user, request.args['From']))
      twiml_response.sms('Twilio Mad Libs: this phone number has been registered as %s. Yay!' % user)
    except sqlite3.IntegrityError as e:
      if str(e).startswith('column name'):
        twiml_response.sms('Twilio Mad Libs: This username (%s) is already registered. Please try another username.' % user)
      elif str(e).startswith('column phone'):
        twiml_response.sms('Twilio Mad Libs: This phone number is already registered. Text "unregister" back to clear our record for this phone number.')

  elif text_body.lower().startswith('unregister'):
    cursor.execute('delete from users where phone == ?', (request.args['From'],))
    twiml_response.sms('This phone number has been removed from Twilio Mad Libs. Thanks for playing!')

  else:
    twiml_response.sms('Twilio Mad Libs: We did not understand that request.')

  conn.commit()
  cursor.close()
  conn.close()
  return twiml_response.toxml()

#####
# Main
#####

if __name__ == '__main__':
  # TODO: If server drops, all in progress libs are marked timed out on restart.
  conn = sqlite3.connect('madlibs.db')
  cursor = conn.cursor()
  initialize_testdb(conn, cursor)
  #app.run(host='0.0.0.0')
  app.run()





# Old shit I don't need.
#def send_confirmation_email(action, user, phone):
#  lines = []
#  url = 'http://127.0.0.1:5000/user_confirm?action=%s&user=%s' % (action, user)
#  if action == 'add':
#    url += '&phone=%s' % phone
#    url += '&auth=%s' % hashlib.md5(SALT + datetime.date.isoformat(datetime.date.today()) + action + user + phone).hexdigest()
#    lines.append('Please click the following link to activate your account.')
#  elif action == 'del':
#    url += '&auth=%s' % hashlib.md5(SALT + datetime.date.isoformat(datetime.date.today()) + action + user).hexdigest()
#    lines.append('Please click the following link to disable your account.')
#
#  lines.append(url)
#
#  msg = MIMEText('\n'.join(lines))
#  msg['Subject'] = 'Confirm user update for Twilio Mad Libs'
#  msg['To'] = user + '@twilio.com'
#  # Send the email via a local SMTP server.
#  #s = smtplib.SMTP('localhost')
#  #s.ehlo()
#  #s.sendmail('donotreply@twiliomadlibs.com', user+'@twilio.com', msg.as_string())
#  #s.quit()
#  s = smtplib.SMTP('smtp.gmail.com')
#  s.ehlo()
#  s.starttls()
#  s.ehlo()
#  s.login('tf2toolbox@gmail.com', 'TF2Toolbox to the world :)')
#  s.sendmail('donotreply@twiliomadlibs.com', 'charlie@twilio.com', msg.as_string())
#  s.quit()

#@app.route('/user_confirm')
#def confirm_user():
#  """
#  These URLs are sent as user confirmation emails. Clicking on them, assuming the
#  URL is correct, will create or delete an user.
#  """
#  if 'auth' not in request.args or 'user' not in request.args or \
#     'action' not in request.args or request.args['action'] not in ['add', 'del'] or \
#     (request.args['action'] == 'add' and 'phone' not in request.args):
#    return 'Invalid user confirmation URL'
#
#  today_string = datetime.date.isoformat(datetime.date.today())
#  hash_string = SALT + today_string + request.args['action'] + request.args['user']
#  if request.args['action'] == 'add':
#    hash_string += request.args['phone']
#
#  auth_hash = hashlib.md5(hash_string).hexdigest()
#
#  if auth_hash != request.args['auth']:
#    return 'You are not authorized to do this.'
#
#  conn = sqlite3.connect(app.config['DB_NAME'])
#  cursor = conn.cursor()
#
#  # NOTE: sqlite3.IntegrityError: column phone is not unique - should catch this.
#  status = ''
#  if request.args['action'] == 'add':
#    cursor.execute('insert or replace into users values (?, ?, 0, 1)', (request.args['user'], request.args['phone']))
#    status = 'Successfully enabled user %s with phone %s\n' % (request.args['user'], request.args['phone'])
#  elif request.args['action'] == 'del':
#    cursor.execute('update or ignore users set active = 0 where name == ?', request.args['user'])
#    status = 'Successfully disabled user %s\n' % request.args['user']
#
#  conn.commit()
#  cursor.close()
#  conn.close()
#  return status + 'Return to the home page: http://127.0.0.1:5000/'

#

#def send_newlib(Lib):
#  """
#  Assumes that the lib recipe has the same number of fields as participants.
#  Randomly assigns a recipe field to each participant. Sends them the text message.
#  Substitutes the {field} for the assigned participant -> {adjective} -> {charlie}
#  """
#  participants = Lib['participants']
#  random.shuffle(participants)
#  fields = parse_fields(Lib['recipe'])
#  assert len(participants) == len(fields)
#
#  new_recipe = Lib['recipe']
#  for participant in participants:
#    assigned_field = fields.pop()
#    text_body = 'Twilio Mad Libs! Please text me back with a: %s' % assigned_field
#    CLIENT.sms.messages.create(to=USERS[participants], from_=app.config['TWILIO_TXT_NUMBER'], body=text_body)
#    new_recipe.replace('{%s}' % assigned_field, '{%s}' % participant, 1)
#
#  # TODO: Update the DB result here with the new recipe.
#  # TODO: Kick off threading.Timer.start() with a timeout function.
#
#def send_final(lib):
#  """
#  Sends the final result string to all participants, split into 160 char chunks.
#  """
#  # Split the string into 160char chunks.
#  messages = []
#  temp_result = lib['result']
#  while len(temp_result > 160):
#    messages.append(temp_result[:160])
#    temp_result = temp_result[160:]
#  messages.append(temp_result)
#
#  for message in messages:
#    for user in lib['participants']:
#      CLIENT.sms.messages.create(to=USERS[user], from_=app.config['TWILIO_TXT_NUMBER'], body=lib['result'])

#

#@app.route('/user_mod', methods=['GET', 'POST'])
#def user_mod_page():
#  """
#  View for the user to signup or quit.
#  """
#  if request.method == 'GET':
#    return render_template('user_mod.html')
#  elif request.method == 'POST':
#    send_confirmation_email(request.form['action'], request.form['user'], request.form['phone'])
#    return render_template('user_mod.html', alert='A confirmation email has been sent to %s' % (request.form['user'] + '@twilio.com'))
#
