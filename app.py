import os
from flask import Flask, render_template, request, redirect, url_for, session
import sqlite3
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'it-is-a-secret'

# Configuration para sa File Uploads
UPLOAD_FOLDER = 'static/posters'
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)

def get_db_connection():
    conn = sqlite3.connect('users.db')
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db_connection()
    # Table para sa Users
    conn.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, 
            name TEXT, 
            email TEXT UNIQUE, 
            password TEXT, 
            role TEXT DEFAULT "user"
        )
    ''')
    # Table para sa Movies (naay Genre column para sa categories)
    conn.execute('''
        CREATE TABLE IF NOT EXISTS movies (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            image TEXT NOT NULL,
            status TEXT NOT NULL,
            genre TEXT NOT NULL, 
            trailer_link TEXT NOT NULL,
            description TEXT,
            cinema_name TEXT,
            showtimes TEXT,
            show_date TEXT
        )
    ''')
    # Siguroha nga naay Admin account
    admin_exists = conn.execute('SELECT * FROM users WHERE email = ?', ("admin@cinemiqu.com",)).fetchone()
    if not admin_exists:
        conn.execute('INSERT INTO users (name, email, password, role) VALUES (?, ?, ?, ?)',
                     ('Admin Dyn', 'admin@cinemiqu.com', 'admin123', 'admin'))
    conn.commit()
    conn.close()

init_db()

# --- NAVIGATION ROUTES ---

@app.route('/')
def home():
    if 'role' in session and session.get('role') == 'admin':
        return redirect(url_for('admin_dashboard'))
    return render_template('index.html', name=session.get('user_name'))

@app.route('/movies')
def movies_page():
    if 'user_id' not in session:
        return redirect(url_for('signin_page'))
    conn = get_db_connection()
    movies = conn.execute('SELECT * FROM movies').fetchall()
    conn.close()
    return render_template('movies.html', name=session.get('user_name'), movies=movies)

@app.route('/categories')
def categories():
    if 'user_id' not in session:
        return redirect(url_for('signin_page'))
    conn = get_db_connection()
    movies = conn.execute('SELECT * FROM movies').fetchall()
    conn.close()
    return render_template('categories.html', name=session.get('user_name'), movies=movies)

@app.route('/movie/<int:movie_id>')
def movie_details(movie_id):
    if 'user_id' not in session:
        return redirect(url_for('signin_page'))
    conn = get_db_connection()
    movie = conn.execute('SELECT * FROM movies WHERE id = ?', (movie_id,)).fetchone()
    conn.close()
    if movie is None:
        return "Movie not found", 404
    return render_template('movie_details.html', name=session.get('user_name'), movie=movie)

@app.route('/about')
def about():
    return render_template('about.html', name=session.get('user_name'))

# --- AUTHENTICATION ROUTES (LOGIN / SIGNUP) ---

@app.route('/signin')
def signin_page():
    return render_template('signin.html')

@app.route('/signup')
def signup_page():
    return render_template('signup.html')


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

        # Check sa role para sa saktong destination
        if user['role'] == 'admin':
            return redirect(url_for('admin_dashboard'))
        else:
            # Kani ang mo-redirect sa normal user padulong sa Home/Index
            return redirect(url_for('home'))

    return "Invalid email or password."

@app.route('/signup_process', methods=['POST'])
def signup_process():
    name = request.form.get('name')
    email = request.form.get('email')
    password = request.form.get('password')
    try:
        conn = get_db_connection()
        conn.execute('INSERT INTO users (name, email, password, role) VALUES (?, ?, ?, ?)',
                     (name, email, password, 'user'))
        conn.commit()
        conn.close()
        return redirect(url_for('signin_page'))
    except sqlite3.IntegrityError:
        return "Email already exists!"

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))

# --- ADMIN ROUTES (MANAGE MOVIES) ---

@app.route('/admin')
def admin_dashboard():
    if session.get('role') != 'admin':
        return "Access Denied!", 403
    conn = get_db_connection()
    movies = conn.execute('SELECT * FROM movies').fetchall()
    conn.close()
    return render_template('admindashboard.html', name=session.get('user_name'), movies=movies)

@app.route('/admin/movies')
def admin_movies():
    if session.get('role') != 'admin':
        return redirect(url_for('signin_page'))
    conn = get_db_connection()
    movies = conn.execute('SELECT * FROM movies').fetchall()
    conn.close()
    return render_template('admin_movies.html', name=session.get('user_name'), movies=movies)


@app.route('/add_movie', methods=['POST'])
def add_movie():
    if session.get('role') != 'admin':
        return redirect(url_for('signin_page'))

    title = request.form.get('title')
    genre = request.form.get('genre')
    status = request.form.get('status')
    trailer = request.form.get('trailer')
    description = request.form.get('description')
    cinema = request.form.get('cinema')
    showtimes = request.form.get('showtimes')
    show_date = request.form.get('show_date')

    file = request.files.get('image_file')
    if file and file.filename != '':
        filename = secure_filename(file.filename)
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        image_db_path = 'posters/' + filename
    else:
        image_db_path = 'posters/default.jpg'

    conn = get_db_connection()
    conn.execute('''INSERT INTO movies 
                    (title, image, status, genre, trailer_link, description, cinema_name, showtimes, show_date) 
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                 (title, image_db_path, status, genre, trailer, description, cinema, showtimes, show_date))
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
    return redirect(url_for('admin_movies'))


@app.route('/edit_movie/<int:id>', methods=['POST'])
def edit_movie(id):
    if session.get('role') != 'admin':
        return redirect(url_for('signin_page'))

    # Pagkuha sa tanang data gikan sa form
    title = request.form.get('title')
    genre = request.form.get('genre')
    status = request.form.get('status')
    trailer = request.form.get('trailer')
    description = request.form.get('description')
    cinema = request.form.get('cinema')
    showtimes = request.form.get('showtimes')
    show_date = request.form.get('show_date')
    file = request.files.get('image_file')

    conn = get_db_connection()

    if file and file.filename != '':
        # Kon naay bag-ong image gi-upload
        filename = secure_filename(file.filename)
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        image_path = 'posters/' + filename

        conn.execute('''UPDATE movies SET title=?, genre=?, status=?, trailer_link=?, 
                        description=?, cinema_name=?, showtimes=?, show_date=?, image=? WHERE id=?''',
                     (title, genre, status, trailer, description, cinema, showtimes, show_date, image_path, id))
    else:
        # Kon karaan ra nga image ang gamiton
        conn.execute('''UPDATE movies SET title=?, genre=?, status=?, trailer_link=?, 
                        description=?, cinema_name=?, showtimes=?, show_date=? WHERE id=?''',
                     (title, genre, status, trailer, description, cinema, showtimes, show_date, id))

    conn.commit()
    conn.close()
    return redirect(url_for('admin_movies'))

@app.route('/edit_movie_page/<int:id>')
def edit_movie_page(id):
    if session.get('role') != 'admin':
        return redirect(url_for('signin_page'))
    conn = get_db_connection()
    movie = conn.execute('SELECT * FROM movies WHERE id = ?', (id,)).fetchone()
    conn.close()
    return render_template('edit_movie.html', movie=movie, name=session.get('user_name'))

if __name__ == '__main__':
    app.run(debug=True)