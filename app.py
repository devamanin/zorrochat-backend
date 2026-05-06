import gevent.monkey
gevent.monkey.patch_all()

from flask import Flask, request, jsonify
from flask_socketio import SocketIO, join_room, leave_room, emit
from flask_cors import CORS
import string
import random
import json
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)
app.config['SECRET_KEY'] = 'supersecret!'
socketio = SocketIO(app, cors_allowed_origins="*")

DB_FILE = 'db.json'

# Initialize in-memory data stores
groups = {}  
messages = {}  
user_chats = {}  
group_members = {}  

def load_data():
    global groups, messages, user_chats, group_members
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f:
                data = json.load(f)
                groups = data.get('groups', {})
                messages = data.get('messages', {})
                user_chats = data.get('user_chats', {})
                # group_members contains sets, but JSON stores lists, so convert back
                gm = data.get('group_members', {})
                group_members = {k: set(v) for k, v in gm.items()}
        except Exception as e:
            print(f"Error loading data: {e}")

def save_data():
    data = {
        'groups': groups,
        'messages': messages,
        'user_chats': user_chats,
        'group_members': {k: list(v) for k, v in group_members.items()}
    }
    try:
        with open(DB_FILE, 'w') as f:
            json.dump(data, f)
    except Exception as e:
        print(f"Error saving data: {e}")

load_data()

# ─── REST API ───────────────────────────────────────────────

@app.route('/api/groups', methods=['GET'])
def get_groups():
    public_groups = [g for g in groups.values() if g.get('type') == 'Public']
    return jsonify(public_groups)

@app.route('/api/groups', methods=['POST'])
def create_group():
    data = request.json
    name = data.get('name')
    group_type = data.get('type')
    group_id = data.get('id')
    creator_id = data.get('creatorId')
    
    if not group_id:
        group_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=7))
    
    print(f"Creating group: {name} with ID: {group_id}")
    group = {'id': group_id, 'name': name, 'type': group_type}
    groups[group_id] = group
    
    if creator_id:
        if group_id not in group_members:
            group_members[group_id] = set()
        group_members[group_id].add(creator_id)
        
    save_data()
    return jsonify(group), 201

@app.route('/api/groups/<group_id>', methods=['GET'])
def get_group_info(group_id):
    print(f"Searching for group with ID: {group_id}")
    print(f"Available group IDs: {list(groups.keys())}")
    group = groups.get(group_id)
    if group:
        print(f"Group found: {group['name']}")
        return jsonify(group)
    print(f"Group NOT found for ID: {group_id}")
    return jsonify({'error': 'Group not found'}), 404

@app.route('/api/groups/<group_id>', methods=['PUT'])
def update_group_name(group_id):
    data = request.json
    new_name = data.get('name')
    if not new_name:
        return jsonify({'error': 'Name is required'}), 400
        
    group = groups.get(group_id)
    if group:
        group['name'] = new_name
        print(f"Group {group_id} name updated to: {new_name}")
        
        # Notify all members of the group that the name changed
        socketio.emit('group_updated', {'roomId': group_id, 'name': new_name}, to=group_id)
        
        save_data()
        return jsonify(group)
    return jsonify({'error': 'Group not found'}), 404

@app.route('/api/groups/<group_id>/members', methods=['GET'])
def get_group_members(group_id):
    """Fetch all known members of a group."""
    members = list(group_members.get(group_id, set()))
    return jsonify(members)

@app.route('/api/messages/<room_id>', methods=['GET'])
def get_messages(room_id):
    """Fetch all stored messages for a room."""
    room_messages = messages.get(room_id, [])
    return jsonify(room_messages)

@app.route('/api/messages/<room_id>', methods=['DELETE'])
def delete_messages(room_id):
    """Delete all stored messages for a room."""
    if room_id in messages:
        messages[room_id] = []
        print(f"Messages cleared for room: {room_id}")
        
        # Notify clients in the room to clear their chat
        socketio.emit('chat_cleared', {'roomId': room_id}, to=room_id)
        
        save_data()
        
    return jsonify({'success': True}), 200

@app.route('/api/chats/<user_id>', methods=['GET'])
def get_chats(user_id):
    """Fetch all recent chat entries for a user."""
    chats = user_chats.get(user_id, {})
    chat_list = list(chats.values())
    # Sort by timestamp descending (newest first)
    chat_list.sort(key=lambda c: c.get('timestamp', ''), reverse=True)
    return jsonify(chat_list)

# ─── SOCKET.IO EVENTS ──────────────────────────────────────

@socketio.on('connect')
def handle_connect(auth):
    print('User connected:', request.sid)

@socketio.on('register_user')
def handle_register_user(data):
    user_id = data.get('userId')
    if user_id:
        join_room(user_id)
        print(f"User {user_id} registered globally on {request.sid}")

@socketio.on('join_room')
def handle_join_room(data):
    room_id = data.get('roomId')
    user_id = data.get('userId')
    join_room(room_id)
    
    # Track membership for group notifications
    if room_id not in group_members:
        group_members[room_id] = set()
    group_members[room_id].add(user_id)
    save_data()
    
    print(f"User {user_id} ({request.sid}) joined room: {room_id} (members: {group_members[room_id]})")
    emit('user_joined', {'userId': user_id}, to=room_id, include_self=False)

@socketio.on('leave_room')
def handle_leave_room(data):
    room_id = data.get('roomId')
    user_id = data.get('userId')
    leave_room(room_id)
    print(f"User {user_id} ({request.sid}) left active socket room: {room_id}")

@socketio.on('permanently_leave_group')
def handle_permanently_leave(data):
    room_id = data.get('roomId')
    user_id = data.get('userId')
    if room_id in group_members and user_id in group_members[room_id]:
        group_members[room_id].remove(user_id)
        save_data()
        print(f"User {user_id} permanently removed from group members of {room_id}")

@socketio.on('send_message')
def handle_send_message(data):
    room_id = data.get('roomId')
    sender_id = data.get('senderId')
    message_text = data.get('message', '')
    timestamp = data.get('timestamp', datetime.utcnow().isoformat())
    color = data.get('color', '#FFFFFF')
    message_id = data.get('messageId')

    # 1. Store the message on the server
    if room_id not in messages:
        messages[room_id] = []
    msg_obj = {
        'roomId': room_id,
        'messageId': message_id,
        'message': message_text,
        'senderId': sender_id,
        'timestamp': timestamp,
        'color': color,
    }
    messages[room_id].append(msg_obj)
    save_data()
    print(f"Stored message in {room_id}: '{message_text}' from {sender_id} with ID {message_id}")

    # 2. Broadcast to everyone in the room except the sender (real-time)
    emit('receive_message', data, to=room_id, include_self=False)

    # 3. Handle peer-to-peer chats (room_ prefix)
    peer_id = _extract_peer(room_id, sender_id)
    if peer_id:
        # Update sender's recent chats
        if sender_id not in user_chats:
            user_chats[sender_id] = {}
        user_chats[sender_id][room_id] = {
            'peerId': peer_id,
            'roomId': room_id,
            'lastMessage': message_text,
            'timestamp': timestamp,
        }

        # Update peer's recent chats
        if peer_id not in user_chats:
            user_chats[peer_id] = {}
        user_chats[peer_id][room_id] = {
            'peerId': sender_id,
            'roomId': room_id,
            'lastMessage': message_text,
            'timestamp': timestamp,
        }
        save_data()

        # Send global notification to the peer
        print(f"Sending global_notification to '{peer_id}' from '{sender_id}'")
        emit('global_notification', {
            'type': 'new_message',
            'senderId': sender_id,
            'roomId': room_id,
            'messageId': message_id,
            'message': message_text,
            'color': color
        }, to=peer_id)
    else:
        # 4. Handle group chats - notify all known members except sender
        members = group_members.get(room_id, set())
        for member_id in members:
            if member_id != sender_id:
                print(f"Sending group notification to '{member_id}' from '{sender_id}' in '{room_id}'")
                emit('global_notification', {
                    'type': 'new_message',
                    'senderId': sender_id,
                    'roomId': room_id,
                    'messageId': message_id,
                    'message': message_text,
                    'color': color
                }, to=member_id)

@socketio.on('delete_message')
def handle_delete_message(data):
    print(f"--- DELETE MESSAGE CALLED ---")
    print(f"Data received: {data}")
    room_id = data.get('roomId')
    message_id = data.get('messageId')
    
    print(f"Looking for message_id {message_id} in room {room_id}")
    
    if room_id and message_id:
        # Remove from stored messages
        if room_id in messages:
            original_len = len(messages[room_id])
            print(f"Messages before deletion: {messages[room_id]}")
            messages[room_id] = [m for m in messages[room_id] if m.get('messageId') != message_id]
            new_len = len(messages[room_id])
            save_data()
            print(f"Messages after deletion: {messages[room_id]}")
            print(f"Deleted {original_len - new_len} messages from memory.")
        else:
            print(f"Room {room_id} not found in messages dictionary.")
        
        # Broadcast the deletion
        emit('message_deleted', {'roomId': room_id, 'messageId': message_id}, to=room_id)
        print(f"Broadcasted message_deleted to room {room_id}")

        # Also send a global notification so background users update their recentChats
        members = group_members.get(room_id, set())
        print(f"Broadcasting global_notification to members: {members}")
        for member_id in members:
            emit('global_notification', {
                'type': 'delete_message',
                'roomId': room_id,
                'messageId': message_id
            }, to=member_id)

def _extract_peer(room_id, sender_id):
    """Extract the peer's userId from a room_id like 'room_{id1}_{id2}'."""
    if not room_id or not room_id.startswith('room_') or not sender_id:
        return None
    remainder = room_id[5:]  # strip 'room_'
    if remainder.startswith(sender_id + '_'):
        return remainder[len(sender_id) + 1:]
    elif remainder.endswith('_' + sender_id):
        return remainder[:-(len(sender_id) + 1)]
    return None


@socketio.on('typing')
def handle_typing(data):
    room_id = data.get('roomId')
    emit('user_typing', data, to=room_id, include_self=False)

@socketio.on('disconnect')
def handle_disconnect():
    print('User disconnected:', request.sid)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    socketio.run(app, port=port, host='0.0.0.0', allow_unsafe_werkzeug=True, debug=False)

