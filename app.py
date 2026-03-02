from flask import Flask, render_template, request, redirect, url_for, session
import sqlite3

app = Flask(__name__)
app.secret_key = 'it-is-a-secret'


def get_db_connection():
    conn = sqlite3.connect('users.db')
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db_connection()
    conn.execute(
        'CREATE TABLE IF NOT EXISTS users (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, email TEXT UNIQUE, password TEXT)')
    conn.commit()
    conn.close()


init_db()


# KINI ANG DEFAULT PAGE (INIG OPEN NIMO SA 127.0.0.1:5000)
@app.route('/')
def home():
    name = session.get('user_name')
    # Siguroha nga index.html ang imong Avatar Page
    return render_template('index.html', name=name)


@app.route('/signup')
def signup_page():
    return render_template('signup.html')


@app.route('/signup_process', methods=['POST'])
def signup_process():
    name = request.form.get('name')
    email = request.form.get('email')
    password = request.form.get('password')
    confirm_password = request.form.get('confirm_password')

    if password != confirm_password:
        return "Passwords do not match!"

    try:
        conn = get_db_connection()
        conn.execute('INSERT INTO users (name, email, password) VALUES (?, ?, ?)', (name, email, password))
        conn.commit()
        conn.close()
        return redirect(url_for('signin_page'))
    except sqlite3.IntegrityError:
        return "Email already exists!"


@app.route('/signin')
def signin_page():
    return render_template('signin.html')


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
        # Redirect balik sa index page human og login
        return redirect(url_for('movies_page'))
    else:
        return "Invalid email or password."


@app.route('/movies')
def movies_page():
    # Dili siya ka-open sa movies kung wala naka-login
    if 'user_id' not in session:
        return redirect(url_for('signin_page'))

    name = session.get('user_name')
    return render_template('movies.html', name=name)


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('home'))


if __name__ == '__main__':
    app.run(debug=True)