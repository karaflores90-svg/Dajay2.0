import os
from flask import Flask, render_template, request, redirect, url_for, session
import sqlite3
from werkzeug.utils import secure_filename # Importa kini para sa file upload

app = Flask(__name__)
app.secret_key = 'it-is-a-secret'

# I-set ang folder diin i-save ang mga posters
UPLOAD_FOLDER = 'static/posters'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# Siguroha nga ang folder nag-exist na daan
if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)


def get_db_connection():
    conn = sqlite3.connect('users.db')
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    conn.execute(
        'CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT UNIQUE, password TEXT, role TEXT DEFAULT "user")')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            image TEXT NOT NULL,
            status TEXT NOT NULL,
            trailer_link TEXT NOT NULL
        )
    ''')
    admin_exists = conn.execute('SELECT * FROM users WHERE email = ?', ("admin@cinemiqu.com",)).fetchone()
    if not admin_exists:
        conn.execute('INSERT INTO users (name, email, password, role) VALUES (?, ?, ?, ?)',
                     ('Admin Dyn', 'admin@cinemiqu.com', 'admin123', 'admin'))
    conn.commit()
    conn.close()


init_db()


@app.route('/')
def home():
    if 'role' in session:
        if session.get('role') == 'admin':
            return redirect(url_for('admin_dashboard'))
    name = session.get('user_name')
    return render_template('index.html', name=name)


@app.route('/movies')
def movies_page():
    if 'user_id' not in session:
        return redirect(url_for('signin_page'))

    conn = get_db_connection()
    # Atong kuhaon tanang movies gikan sa database
    movies = conn.execute('SELECT * FROM movies').fetchall()
    conn.close()

    # I-pasa ang 'movies' variable ngadto sa template
    return render_template('movies.html', name=session.get('user_name'), movies=movies)


@app.route('/admin')
def admin_dashboard():
    if session.get('role') != 'admin':
        return "Access Denied!", 403
    conn = get_db_connection()
    movies = conn.execute('SELECT * FROM movies').fetchall()
    conn.close()
    return render_template('admindashboard.html', name=session.get('user_name'), movies=movies)


@app.route('/add_movie', methods=['POST'])
def add_movie():
    if session.get('role') != 'admin':
        return redirect(url_for('signin_page'))

    title = request.form.get('title')
    status = request.form.get('status')
    trailer = request.form.get('trailer')

    # Pag-handle sa File Upload imbes nga text input
    file = request.files.get('image_file')
    if file and file.filename != '':
        filename = secure_filename(file.filename)
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        # I-save ang relative path para dali i-load sa HTML
        image_db_path = 'posters/' + filename
    else:
        image_db_path = 'default.jpg'

    conn = get_db_connection()
    conn.execute('INSERT INTO movies (title, image, status, trailer_link) VALUES (?, ?, ?, ?)',
                 (title, image_db_path, status, trailer))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_dashboard'))


@app.route('/delete_movie/<int:id>')
def delete_movie(id):
    if session.get('role') != 'admin':
        return redirect(url_for('signin_page'))
    conn = get_db_connection()
    conn.execute('DELETE FROM movies WHERE id = ?', (id,))
    conn.commit()
    conn.close()
    return redirect(url_for('admin_dashboard'))


@app.route('/signup_process', methods=['POST'])
def signup_process():
    name = request.form.get('name')
    email = request.form.get('email')
    password = request.form.get('password')
    confirm_password = request.form.get('confirm_password')
    if password != confirm_password: return "Passwords do not match!"
    try:
        conn = get_db_connection()
        conn.execute('INSERT INTO users (name, email, password, role) VALUES (?, ?, ?, ?)',
                     (name, email, password, 'user'))
        conn.commit()
        conn.close()
        return redirect(url_for('signin_page'))
    except sqlite3.IntegrityError:
        return "Email already exists!"


@app.route('/login_process', methods=['POST'])
def login_process():
    email = request.form.get('email')
    password = request.form.get('password')
    conn = get_db_connection()
    user = conn.execute('SELECT * FROM users WHERE email = ? AND password = ?', (email, password)).fetchone()
    conn.close()
    if user:
        session['user_id'] = user['id']
        session['user_name'] = user['name']
        session['role'] = user['role']
        return redirect(url_for('admin_dashboard') if user['role'] == 'admin' else url_for('movies_page'))
    return "Invalid email or password."


@app.route('/signin')
def signin_page(): return render_template('signin.html')


@app.route('/signup')
def signup_page(): return render_template('signup.html')


@app.route('/about')
def about(): return render_template('about.html', name=session.get('user_name'))


@app.route('/categories')
def categories(): return render_template('categories.html', name=session.get('user_name'))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))


if __name__ == '__main__':
    app.run(debug=True)