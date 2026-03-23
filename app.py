import os
import sqlite3
import base64
import hashlib
import hmac
import json
import time
from collections import defaultdict
from decimal import Decimal, InvalidOperation
from datetime import datetime, timedelta
from urllib import error as urllib_error
from urllib import request as urllib_request

from flask import Flask, flash, jsonify, redirect, render_template, request, session, url_for
from werkzeug.utils import secure_filename


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_env_file(env_filename=".env"):
    env_path = os.path.join(BASE_DIR, env_filename)
    if not os.path.exists(env_path):
        return

    with open(env_path, "r", encoding="utf-8") as env_file:
        for raw_line in env_file:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].strip()
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip()

            if not key:
                continue

            if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
                value = value[1:-1]

            os.environ[key] = value


load_env_file()


app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY") or "it-is-a-secret"

UPLOAD_FOLDER = "static/posters"
ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MOVIE_GENRES = ("Sci-Fi", "Horror", "Animation", "Action", "Drama", "Comedy", "Thriller")
MOVIE_STATUSES = ("Now Showing", "Coming Soon", "Ended", "Archived")
PAYMENT_STATUSES = ("Pending", "Paid", "Cancelled", "Refunded")
DEFAULT_RUNTIME_MINUTES = 120
DEFAULT_HALL_NAME = "Screen 1"
SEAT_ROWS = ("A", "B", "C", "D", "E")
SEATS_PER_ROW = 8
PAYMONGO_API_BASE_URL = "https://api.paymongo.com/v1"

app.config["UPLOAD_FOLDER"] = UPLOAD_FOLDER

if not os.path.exists(UPLOAD_FOLDER):
    os.makedirs(UPLOAD_FOLDER)


def get_db_connection():
    conn = sqlite3.connect("users.db")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def current_timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def current_date_value():
    return datetime.now().strftime("%Y-%m-%d")


def current_month_value():
    return datetime.now().strftime("%Y-%m")


def build_booking_reference(booking_id):
    return f"BK-{booking_id:05d}"


def format_month_label(month_value):
    if not month_value:
        return "This Month"
    try:
        return datetime.strptime(month_value, "%Y-%m").strftime("%B %Y")
    except ValueError:
        return month_value


def format_display_date(date_value):
    if not date_value:
        return "To Be Announced"
    try:
        return datetime.strptime(date_value, "%Y-%m-%d").strftime("%b %d, %Y")
    except ValueError:
        return date_value


def parse_time_value(time_value):
    cleaned_value = " ".join((time_value or "").strip().upper().replace(".", "").split())
    if not cleaned_value:
        raise ValueError("Time value is empty.")

    for time_format in ("%H:%M", "%I:%M %p", "%I %p", "%I:%M%p"):
        try:
            return datetime.strptime(cleaned_value, time_format)
        except ValueError:
            continue

    raise ValueError(f"Invalid time value: {time_value}")


def normalize_time_value(time_value):
    return parse_time_value(time_value).strftime("%H:%M")


def format_display_time(time_value):
    if not time_value:
        return "TBA"
    try:
        return parse_time_value(time_value).strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return time_value


def parse_time_to_minutes(time_value):
    parsed_time = parse_time_value(time_value)
    return parsed_time.hour * 60 + parsed_time.minute


def try_parse_time_to_minutes(time_value):
    try:
        return parse_time_to_minutes(time_value)
    except ValueError:
        return None


def get_time_sort_minutes(time_value, invalid_minutes=24 * 60):
    # Legacy rows may contain placeholders like "N/A"; sort them after real showtimes.
    parsed_minutes = try_parse_time_to_minutes(time_value)
    if parsed_minutes is None:
        return invalid_minutes
    return parsed_minutes


def build_seat_labels():
    seat_labels = []
    for row_label in SEAT_ROWS:
        for seat_number in range(1, SEATS_PER_ROW + 1):
            seat_labels.append(f"{row_label}{seat_number}")
    return seat_labels


ALL_SEAT_LABELS = build_seat_labels()
TOTAL_SEAT_COUNT = len(ALL_SEAT_LABELS)


def split_csv_values(raw_value):
    if not raw_value:
        return []
    return [value.strip() for value in raw_value.split(",") if value.strip()]


def split_people_values(raw_value):
    if not raw_value:
        return []
    normalized = raw_value.replace("\n", ",")
    return [value.strip() for value in normalized.split(",") if value.strip()]


def normalize_schedule_values(raw_value):
    normalized_times = []
    invalid_times = []

    for raw_time in split_csv_values(raw_value):
        try:
            normalized_times.append(normalize_time_value(raw_time))
        except ValueError:
            invalid_times.append(raw_time)

    return normalized_times, invalid_times


def allowed_image(filename):
    if "." not in filename:
        return False
    return filename.rsplit(".", 1)[1].lower() in ALLOWED_IMAGE_EXTENSIONS


def safe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def format_php_amount(amount_in_centavos):
    if amount_in_centavos is None:
        return "TBA"
    return f"PHP {amount_in_centavos / 100:,.2f}"


def get_paymongo_secret_key():
    secret_key = (os.environ.get("PAYMONGO_SECRET_KEY") or "").strip()
    if not secret_key:
        raise ValueError("PAYMONGO_SECRET_KEY is not configured.")
    return secret_key


def get_paymongo_payment_method_types():
    configured_methods = (os.environ.get("PAYMONGO_PAYMENT_METHOD_TYPES") or "gcash").strip()
    payment_method_types = [value.strip().lower() for value in configured_methods.split(",") if value.strip()]
    if not payment_method_types:
        raise ValueError("PAYMONGO_PAYMENT_METHOD_TYPES must contain at least one payment method.")
    return payment_method_types


def get_paymongo_webhook_secret():
    return (os.environ.get("PAYMONGO_WEBHOOK_SECRET") or "").strip()


def get_ticket_price_centavos():
    raw_value = (os.environ.get("PAYMONGO_TICKET_PRICE_PHP") or "").strip()
    if not raw_value:
        raise ValueError("PAYMONGO_TICKET_PRICE_PHP is not configured.")

    try:
        ticket_price = Decimal(raw_value)
    except InvalidOperation as exc:
        raise ValueError("PAYMONGO_TICKET_PRICE_PHP must be a valid number.") from exc

    if ticket_price <= 0:
        raise ValueError("PAYMONGO_TICKET_PRICE_PHP must be greater than zero.")

    return int((ticket_price * 100).quantize(Decimal("1")))


def get_app_base_url():
    configured_base_url = (os.environ.get("APP_BASE_URL") or "").strip().rstrip("/")
    if configured_base_url:
        return configured_base_url
    return request.url_root.rstrip("/")


def parse_paymongo_error_response(response_text, fallback_message):
    if not response_text:
        return fallback_message

    try:
        payload = json.loads(response_text)
    except json.JSONDecodeError:
        return response_text.strip() or fallback_message

    errors = payload.get("errors") or []
    if errors:
        detail = errors[0].get("detail") or errors[0].get("code")
        if detail:
            return detail

    attributes = ((payload.get("data") or {}).get("attributes") or {})
    detail = attributes.get("message")
    if detail:
        return detail

    return fallback_message


def paymongo_api_request(method, path, payload=None):
    secret_key = get_paymongo_secret_key()
    request_url = f"{PAYMONGO_API_BASE_URL}{path}"
    encoded_credentials = base64.b64encode(f"{secret_key}:".encode("utf-8")).decode("utf-8")
    request_body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request_headers = {
        "Authorization": f"Basic {encoded_credentials}",
        "Accept": "application/json",
    }

    if payload is not None:
        request_headers["Content-Type"] = "application/json"

    req = urllib_request.Request(request_url, data=request_body, headers=request_headers, method=method.upper())

    try:
        with urllib_request.urlopen(req, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib_error.HTTPError as exc:
        response_text = exc.read().decode("utf-8", errors="replace")
        error_message = parse_paymongo_error_response(response_text, "PayMongo rejected the request.")
        raise RuntimeError(error_message) from exc
    except urllib_error.URLError as exc:
        raise RuntimeError("Could not reach PayMongo. Please try again in a moment.") from exc


def get_booking_checkout_context(conn, booking_id, user_id=None):
    query = """
        SELECT
            b.*,
            u.name AS user_name,
            u.email AS user_email,
            m.title,
            m.cinema_name,
            st.schedule_date,
            st.schedule_time,
            st.hall_name
        FROM bookings b
        JOIN users u ON u.id = b.user_id
        JOIN movies m ON m.id = b.movie_id
        JOIN showtimes st ON st.id = b.showtime_id
        WHERE b.id = ?
    """
    params = [booking_id]

    if user_id is not None:
        query += " AND b.user_id = ?"
        params.append(user_id)

    return conn.execute(query, params).fetchone()


def build_paymongo_checkout_payload(booking_row, minimal=False):
    total_amount = booking_row["payment_amount"] or 0
    ticket_quantity = booking_row["ticket_quantity"] or 1
    if total_amount <= 0 or ticket_quantity <= 0:
        raise ValueError("Booking amount is invalid for PayMongo checkout.")

    unit_amount = max(total_amount // ticket_quantity, 1)
    base_url = get_app_base_url()
    booking_id = booking_row["id"]
    booking_reference = booking_row["booking_reference"] or build_booking_reference(booking_id)
    payment_methods = get_paymongo_payment_method_types()
    billing = {}

    if booking_row["user_name"]:
        billing["name"] = booking_row["user_name"]
    if booking_row["user_email"]:
        billing["email"] = booking_row["user_email"]

    attributes = {
        "cancel_url": f"{base_url}{url_for('paymongo_checkout_cancel', booking_id=booking_id)}",
        "success_url": f"{base_url}{url_for('paymongo_checkout_success', booking_id=booking_id)}",
        "description": f"Movie booking {booking_reference}" if not minimal else "Movie checkout",
        "line_items": [
            {
                "amount": unit_amount,
                "currency": "PHP",
                "name": "Movie Ticket",
                "quantity": ticket_quantity,
            }
        ],
        "payment_method_types": payment_methods,
    }

    if not minimal:
        attributes["reference_number"] = booking_reference
        attributes["send_email_receipt"] = False
        attributes["show_description"] = True
        attributes["show_line_items"] = True
        attributes["metadata"] = {
            "booking_id": str(booking_id),
            "booking_reference": booking_reference,
        }

    if billing and not minimal:
        attributes["billing"] = billing

    return {"data": {"attributes": attributes}}


def create_paymongo_checkout_session(booking_row):
    payload = build_paymongo_checkout_payload(booking_row)
    try:
        return paymongo_api_request("POST", "/checkout_sessions", payload)
    except RuntimeError as exc:
        fallback_payload = build_paymongo_checkout_payload(booking_row, minimal=True)
        try:
            return paymongo_api_request("POST", "/checkout_sessions", fallback_payload)
        except RuntimeError:
            raise exc


def retrieve_paymongo_checkout_session(checkout_session_id):
    return paymongo_api_request("GET", f"/checkout_sessions/{checkout_session_id}")


def expire_paymongo_checkout_session(checkout_session_id):
    return paymongo_api_request("POST", f"/checkout_sessions/{checkout_session_id}/expire")


def sync_booking_with_checkout_session(conn, booking_id, checkout_payload):
    checkout_data = checkout_payload.get("data") or {}
    checkout_attributes = checkout_data.get("attributes") or {}
    checkout_session_id = checkout_data.get("id")
    checkout_status = checkout_attributes.get("status") or "unknown"
    checkout_url = checkout_attributes.get("checkout_url")
    checkout_reference = checkout_attributes.get("reference_number")
    payments = checkout_attributes.get("payments") or []

    paid_payment = None
    for payment in payments:
        payment_attributes = payment.get("attributes") or {}
        if payment_attributes.get("status") in ("paid", "succeeded"):
            paid_payment = payment
            break

    payment_status = "Paid" if paid_payment or checkout_status in ("paid", "completed") else "Pending"
    payment_reference = checkout_reference
    payment_paid_at = None

    if paid_payment:
        payment_attributes = paid_payment.get("attributes") or {}
        payment_reference = paid_payment.get("id") or checkout_reference
        paid_at_value = safe_int(payment_attributes.get("paid_at"))
        if paid_at_value:
            payment_paid_at = datetime.fromtimestamp(paid_at_value).strftime("%Y-%m-%d %H:%M:%S")

    conn.execute(
        """
        UPDATE bookings
        SET payment_status = ?,
            checkout_session_id = COALESCE(?, checkout_session_id),
            checkout_url = COALESCE(?, checkout_url),
            checkout_status = ?,
            payment_reference = COALESCE(?, payment_reference),
            payment_paid_at = COALESCE(?, payment_paid_at),
            updated_at = ?
        WHERE id = ?
        """,
        (
            payment_status,
            checkout_session_id,
            checkout_url,
            checkout_status,
            payment_reference,
            payment_paid_at,
            current_timestamp(),
            booking_id,
        ),
    )

    return {
        "checkout_session_id": checkout_session_id,
        "checkout_status": checkout_status,
        "checkout_url": checkout_url,
        "is_paid": payment_status == "Paid",
        "payment_reference": payment_reference,
    }


def parse_paymongo_signature_header(signature_header):
    signature_parts = {}
    for raw_part in (signature_header or "").split(","):
        if "=" not in raw_part:
            continue
        key, value = raw_part.split("=", 1)
        signature_parts[key.strip()] = value.strip()
    return signature_parts


def verify_paymongo_webhook_signature(signature_header, raw_payload):
    webhook_secret = get_paymongo_webhook_secret()
    if not webhook_secret:
        return None
    signature_parts = parse_paymongo_signature_header(signature_header)
    timestamp = signature_parts.get("t")

    if not timestamp:
        return False

    tolerance_seconds = safe_int(os.environ.get("PAYMONGO_WEBHOOK_TOLERANCE_SECONDS")) or 300
    current_time = int(time.time())
    request_time = safe_int(timestamp)
    if request_time is None or abs(current_time - request_time) > tolerance_seconds:
        return False

    signed_payload = timestamp.encode("utf-8") + b"." + raw_payload
    expected_signature = hmac.new(
        webhook_secret.encode("utf-8"),
        signed_payload,
        hashlib.sha256,
    ).hexdigest()

    for signature_key in ("te", "li"):
        header_signature = signature_parts.get(signature_key)
        if header_signature and hmac.compare_digest(expected_signature, header_signature):
            return True

    return False


def resolve_booking_id_for_checkout_session(conn, checkout_session_data):
    checkout_session_id = checkout_session_data.get("id")
    checkout_attributes = checkout_session_data.get("attributes") or {}
    metadata = checkout_attributes.get("metadata") or {}
    booking_id = safe_int(metadata.get("booking_id"))

    if booking_id is not None:
        exists = conn.execute("SELECT id FROM bookings WHERE id = ?", (booking_id,)).fetchone()
        if exists is not None:
            return booking_id

    if checkout_session_id:
        row = conn.execute(
            "SELECT id FROM bookings WHERE checkout_session_id = ?",
            (checkout_session_id,),
        ).fetchone()
        if row is not None:
            return row["id"]

    booking_reference = metadata.get("booking_reference") or checkout_attributes.get("reference_number")
    if booking_reference:
        row = conn.execute(
            "SELECT id FROM bookings WHERE booking_reference = ?",
            (booking_reference,),
        ).fetchone()
        if row is not None:
            return row["id"]

    return None


def ensure_booking_checkout_session(conn, booking_row):
    checkout_session_id = booking_row["checkout_session_id"]
    checkout_url = booking_row["checkout_url"]

    if checkout_session_id:
        checkout_payload = retrieve_paymongo_checkout_session(checkout_session_id)
        sync_result = sync_booking_with_checkout_session(conn, booking_row["id"], checkout_payload)
        if sync_result["is_paid"]:
            return sync_result
        if sync_result["checkout_status"] == "active" and sync_result["checkout_url"]:
            return sync_result

    checkout_payload = create_paymongo_checkout_session(booking_row)
    sync_result = sync_booking_with_checkout_session(conn, booking_row["id"], checkout_payload)
    if sync_result["checkout_url"]:
        return sync_result

    if checkout_url:
        return {
            "checkout_session_id": checkout_session_id,
            "checkout_status": "active",
            "checkout_url": checkout_url,
            "is_paid": False,
            "payment_reference": booking_row["payment_reference"],
        }

    raise RuntimeError("PayMongo did not return a checkout URL.")


def ensure_column(conn, table_name, column_name, definition):
    existing_columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table_name})")}
    if column_name not in existing_columns:
        conn.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}")


def time_ranges_overlap(start_a, duration_a, start_b, duration_b):
    return start_a < (start_b + duration_b) and start_b < (start_a + duration_a)


def migrate_movies_table(conn):
    conn.execute(
        """
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
        """
    )

    ensure_column(conn, "movies", "actors", "TEXT")
    ensure_column(conn, "movies", "directors", "TEXT")
    ensure_column(conn, "movies", "is_featured", "INTEGER DEFAULT 0")
    ensure_column(conn, "movies", "featured_month", "TEXT")
    ensure_column(conn, "movies", "created_at", "TEXT")
    ensure_column(conn, "movies", "updated_at", "TEXT")
    ensure_column(conn, "movies", "hall_name", f"TEXT DEFAULT '{DEFAULT_HALL_NAME}'")
    ensure_column(conn, "movies", "runtime_minutes", f"INTEGER DEFAULT {DEFAULT_RUNTIME_MINUTES}")

    timestamp = current_timestamp()
    conn.execute(
        """
        UPDATE movies
        SET hall_name = COALESCE(NULLIF(hall_name, ''), ?),
            runtime_minutes = COALESCE(runtime_minutes, ?),
            created_at = COALESCE(created_at, ?),
            updated_at = COALESCE(updated_at, ?),
            is_featured = COALESCE(is_featured, 0)
        """,
        (DEFAULT_HALL_NAME, DEFAULT_RUNTIME_MINUTES, timestamp, timestamp),
    )


def create_showtimes_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS showtimes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            movie_id INTEGER NOT NULL,
            hall_name TEXT NOT NULL DEFAULT 'Screen 1',
            schedule_date TEXT NOT NULL,
            schedule_time TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (movie_id) REFERENCES movies (id) ON DELETE CASCADE
        )
        """
    )
    ensure_column(conn, "showtimes", "hall_name", f"TEXT DEFAULT '{DEFAULT_HALL_NAME}'")

    conn.execute(
        """
        UPDATE showtimes
        SET hall_name = COALESCE(
            NULLIF(hall_name, ''),
            (
                SELECT COALESCE(NULLIF(movies.hall_name, ''), ?)
                FROM movies
                WHERE movies.id = showtimes.movie_id
            ),
            ?
        )
        """,
        (DEFAULT_HALL_NAME, DEFAULT_HALL_NAME),
    )


def create_bookings_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bookings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_reference TEXT,
            user_id INTEGER NOT NULL,
            movie_id INTEGER NOT NULL,
            showtime_id INTEGER NOT NULL,
            ticket_quantity INTEGER NOT NULL,
            seats TEXT NOT NULL,
            payment_status TEXT NOT NULL DEFAULT 'Pending',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
            FOREIGN KEY (movie_id) REFERENCES movies (id) ON DELETE CASCADE,
            FOREIGN KEY (showtime_id) REFERENCES showtimes (id) ON DELETE CASCADE
        )
        """
    )
    ensure_column(conn, "bookings", "booking_reference", "TEXT")
    ensure_column(conn, "bookings", "payment_status", "TEXT DEFAULT 'Pending'")
    ensure_column(conn, "bookings", "updated_at", "TEXT")
    ensure_column(conn, "bookings", "payment_provider", "TEXT")
    ensure_column(conn, "bookings", "payment_amount", "INTEGER")
    ensure_column(conn, "bookings", "checkout_session_id", "TEXT")
    ensure_column(conn, "bookings", "checkout_url", "TEXT")
    ensure_column(conn, "bookings", "checkout_status", "TEXT")
    ensure_column(conn, "bookings", "payment_reference", "TEXT")
    ensure_column(conn, "bookings", "payment_paid_at", "TEXT")

    timestamp = current_timestamp()
    conn.execute(
        """
        UPDATE bookings
        SET payment_status = COALESCE(payment_status, 'Pending'),
            payment_provider = COALESCE(payment_provider, 'PayMongo'),
            checkout_status = COALESCE(checkout_status, 'pending'),
            updated_at = COALESCE(updated_at, created_at, ?)
        """,
        (timestamp,),
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_reference ON bookings(booking_reference)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_bookings_checkout_session ON bookings(checkout_session_id)"
    )


def create_booked_seats_table(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS booked_seats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            booking_id INTEGER NOT NULL,
            showtime_id INTEGER NOT NULL,
            seat_label TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (booking_id) REFERENCES bookings (id) ON DELETE CASCADE,
            FOREIGN KEY (showtime_id) REFERENCES showtimes (id) ON DELETE CASCADE
        )
        """
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_booked_seats_unique ON booked_seats(showtime_id, seat_label)"
    )


def seed_legacy_showtimes(conn):
    movies = conn.execute("SELECT id, show_date, showtimes, hall_name FROM movies").fetchall()
    for movie in movies:
        existing_total = conn.execute(
            "SELECT COUNT(*) AS total FROM showtimes WHERE movie_id = ?",
            (movie["id"],),
        ).fetchone()["total"]

        if existing_total:
            continue

        if not movie["show_date"] or not movie["showtimes"]:
            continue

        schedule_times, invalid_times = normalize_schedule_values(movie["showtimes"])
        if invalid_times or not schedule_times:
            continue

        created_at = current_timestamp()
        for schedule_time in schedule_times:
            conn.execute(
                """
                INSERT INTO showtimes (movie_id, hall_name, schedule_date, schedule_time, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    movie["id"],
                    movie["hall_name"] or DEFAULT_HALL_NAME,
                    movie["show_date"],
                    schedule_time,
                    created_at,
                ),
            )


def backfill_booking_references(conn):
    bookings = conn.execute(
        "SELECT id FROM bookings WHERE booking_reference IS NULL OR booking_reference = ''"
    ).fetchall()

    for booking in bookings:
        conn.execute(
            "UPDATE bookings SET booking_reference = ? WHERE id = ?",
            (build_booking_reference(booking["id"]), booking["id"]),
        )


def sync_movie_schedule_summary(conn, movie_id):
    schedule_rows = conn.execute(
        """
        SELECT hall_name, schedule_date, schedule_time
        FROM showtimes
        WHERE movie_id = ?
        """,
        (movie_id,),
    ).fetchall()

    if not schedule_rows:
        conn.execute(
            """
            UPDATE movies
            SET show_date = NULL,
                showtimes = NULL,
                updated_at = ?
            WHERE id = ?
            """,
            (current_timestamp(), movie_id),
        )
        return

    sorted_rows = sorted(
        schedule_rows,
        key=lambda row: (row["schedule_date"], get_time_sort_minutes(row["schedule_time"]), row["hall_name"] or ""),
    )
    first_date = sorted_rows[0]["schedule_date"]
    first_day_rows = [row for row in sorted_rows if row["schedule_date"] == first_date]
    display_times = ", ".join(format_display_time(row["schedule_time"]) for row in first_day_rows)
    first_hall_name = first_day_rows[0]["hall_name"] or DEFAULT_HALL_NAME

    conn.execute(
        """
        UPDATE movies
        SET show_date = ?,
            showtimes = ?,
            hall_name = COALESCE(NULLIF(hall_name, ''), ?),
            updated_at = ?
        WHERE id = ?
        """,
        (first_date, display_times, first_hall_name, current_timestamp(), movie_id),
    )


def sync_all_movie_summaries(conn):
    movie_rows = conn.execute("SELECT id FROM movies").fetchall()
    for movie in movie_rows:
        sync_movie_schedule_summary(conn, movie["id"])


def validate_showtime_conflicts(
    conn,
    cinema_name,
    hall_name,
    schedule_date,
    schedule_times,
    runtime_minutes,
    exclude_movie_id=None,
):
    errors = []

    if not cinema_name or not hall_name or not schedule_date or not schedule_times:
        return errors

    sorted_input_times = sorted(schedule_times, key=parse_time_to_minutes)
    for index, first_time in enumerate(sorted_input_times):
        for second_time in sorted_input_times[index + 1 :]:
            if time_ranges_overlap(
                parse_time_to_minutes(first_time),
                runtime_minutes,
                parse_time_to_minutes(second_time),
                runtime_minutes,
            ):
                errors.append(
                    f"Conflict inside {hall_name}: {format_display_time(first_time)} overlaps {format_display_time(second_time)}."
                )

    query = """
        SELECT
            st.schedule_time,
            st.hall_name,
            m.title,
            COALESCE(m.runtime_minutes, ?) AS runtime_minutes
        FROM showtimes st
        JOIN movies m ON m.id = st.movie_id
        WHERE st.schedule_date = ?
          AND LOWER(COALESCE(m.cinema_name, '')) = LOWER(?)
          AND LOWER(COALESCE(st.hall_name, '')) = LOWER(?)
    """
    params = [DEFAULT_RUNTIME_MINUTES, schedule_date, cinema_name, hall_name]

    if exclude_movie_id is not None:
        query += " AND st.movie_id != ?"
        params.append(exclude_movie_id)

    existing_rows = conn.execute(query, params).fetchall()
    for schedule_time in sorted_input_times:
        new_start = parse_time_to_minutes(schedule_time)
        for existing_row in existing_rows:
            existing_start = try_parse_time_to_minutes(existing_row["schedule_time"])
            if existing_start is None:
                continue
            existing_runtime = existing_row["runtime_minutes"] or DEFAULT_RUNTIME_MINUTES
            if time_ranges_overlap(new_start, runtime_minutes, existing_start, existing_runtime):
                errors.append(
                    f'{format_display_time(schedule_time)} conflicts with "{existing_row["title"]}" at {format_display_time(existing_row["schedule_time"])} in {hall_name}.'
                )

    unique_errors = []
    for error in errors:
        if error not in unique_errors:
            unique_errors.append(error)
    return unique_errors


def replace_movie_showtimes(conn, movie_id, hall_name, schedule_date, schedule_times):
    conn.execute("DELETE FROM showtimes WHERE movie_id = ?", (movie_id,))
    created_at = current_timestamp()

    if schedule_date and schedule_times:
        for schedule_time in schedule_times:
            conn.execute(
                """
                INSERT INTO showtimes (movie_id, hall_name, schedule_date, schedule_time, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (movie_id, hall_name or DEFAULT_HALL_NAME, schedule_date, schedule_time, created_at),
            )

    sync_movie_schedule_summary(conn, movie_id)


def add_movie_showtimes(conn, movie_id, hall_name, schedule_date, schedule_times):
    created_at = current_timestamp()
    for schedule_time in schedule_times:
        conn.execute(
            """
            INSERT INTO showtimes (movie_id, hall_name, schedule_date, schedule_time, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (movie_id, hall_name or DEFAULT_HALL_NAME, schedule_date, schedule_time, created_at),
        )
    sync_movie_schedule_summary(conn, movie_id)


def release_booking_seats(conn, booking_id):
    conn.execute("DELETE FROM booked_seats WHERE booking_id = ?", (booking_id,))


def init_db():
    conn = get_db_connection()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            email TEXT UNIQUE,
            password TEXT,
            role TEXT DEFAULT 'user'
        )
        """
    )

    migrate_movies_table(conn)
    create_showtimes_table(conn)
    create_bookings_table(conn)
    create_booked_seats_table(conn)
    seed_legacy_showtimes(conn)
    backfill_booking_references(conn)
    sync_all_movie_summaries(conn)

    admin_exists = conn.execute(
        "SELECT id FROM users WHERE email = ?",
        ("admin@cinemiqu.com",),
    ).fetchone()
    if not admin_exists:
        conn.execute(
            "INSERT INTO users (name, email, password, role) VALUES (?, ?, ?, ?)",
            ("Admin Dyn", "admin@cinemiqu.com", "admin123", "admin"),
        )

    conn.commit()
    conn.close()


def normalize_movie_payload(form_data):
    showtime_list, invalid_showtimes = normalize_schedule_values(form_data.get("showtimes"))
    runtime_minutes = safe_int(form_data.get("runtime_minutes"))
    is_featured = 1 if form_data.get("is_featured") == "on" else 0
    featured_month = (form_data.get("featured_month") or "").strip()

    if is_featured and not featured_month:
        featured_month = current_month_value()
    if not is_featured:
        featured_month = None

    return {
        "title": (form_data.get("title") or "").strip(),
        "genre": (form_data.get("genre") or "").strip(),
        "status": (form_data.get("status") or "").strip(),
        "trailer": (form_data.get("trailer") or "").strip(),
        "description": (form_data.get("description") or "").strip(),
        "cinema": (form_data.get("cinema") or "").strip(),
        "hall_name": (form_data.get("hall_name") or "").strip(),
        "show_date": (form_data.get("show_date") or "").strip(),
        "showtime_list": showtime_list,
        "showtimes_text": ", ".join(format_display_time(time_value) for time_value in showtime_list),
        "invalid_showtimes": invalid_showtimes,
        "actors": ", ".join(split_people_values(form_data.get("actors"))),
        "directors": ", ".join(split_people_values(form_data.get("directors"))),
        "is_featured": is_featured,
        "featured_month": featured_month,
        "runtime_minutes": runtime_minutes,
    }


def validate_movie_payload(conn, payload, image_file=None, movie_id=None):
    errors = []

    if not payload["title"]:
        errors.append("Movie title is required.")
    if not payload["genre"]:
        errors.append("Movie genre is required.")
    if payload["status"] not in MOVIE_STATUSES:
        errors.append("Please select a valid listing status.")
    if not payload["trailer"]:
        errors.append("Trailer link is required.")
    if not payload["description"]:
        errors.append("Movie description is required.")
    if not payload["cinema"]:
        errors.append("Cinema name is required.")
    if not payload["hall_name"]:
        errors.append("Hall or screen name is required.")
    if payload["runtime_minutes"] is None or payload["runtime_minutes"] < 30:
        errors.append("Runtime must be at least 30 minutes.")
    if payload["invalid_showtimes"]:
        errors.append(
            "Invalid showtime format: " + ", ".join(payload["invalid_showtimes"]) + ". Use values like 1:00 PM."
        )
    if payload["show_date"] and not payload["showtime_list"]:
        errors.append("Add at least one valid showtime when a show date is provided.")
    if payload["showtime_list"] and not payload["show_date"]:
        errors.append("Provide a show date when adding showtimes.")
    if payload["status"] == "Now Showing" and not payload["showtime_list"]:
        errors.append("Now Showing movies need at least one schedule.")
    if not payload["actors"]:
        errors.append("Please add at least one actor.")
    if not payload["directors"]:
        errors.append("Please add at least one director.")

    if image_file and image_file.filename and not allowed_image(image_file.filename):
        allowed_extensions = ", ".join(sorted(ALLOWED_IMAGE_EXTENSIONS))
        errors.append(f"Poster must be one of: {allowed_extensions}.")

    if not movie_id and (not image_file or not image_file.filename):
        errors.append("Movie poster is required when adding a movie.")

    existing_movie = conn.execute(
        "SELECT id FROM movies WHERE LOWER(title) = LOWER(?)",
        (payload["title"],),
    ).fetchone()
    if existing_movie and existing_movie["id"] != movie_id:
        errors.append("A movie with that title already exists.")

    return errors


def save_image_file(image_file):
    filename = secure_filename(image_file.filename)
    image_path = os.path.join(app.config["UPLOAD_FOLDER"], filename)
    image_file.save(image_path)
    return f"posters/{filename}"


def build_movie_search(search_query, public_view=False):
    conditions = []
    params = []

    if public_view:
        conditions.append("COALESCE(m.status, '') != 'Archived'")

    if search_query:
        search_value = f"%{search_query.lower()}%"
        conditions.append(
            """
            (
                LOWER(m.title) LIKE ?
                OR LOWER(m.genre) LIKE ?
                OR LOWER(m.status) LIKE ?
                OR LOWER(COALESCE(m.cinema_name, '')) LIKE ?
                OR LOWER(COALESCE(m.hall_name, '')) LIKE ?
                OR LOWER(COALESCE(m.actors, '')) LIKE ?
                OR LOWER(COALESCE(m.directors, '')) LIKE ?
            )
            """
        )
        params.extend([search_value] * 7)

    if not conditions:
        return "", []

    return " WHERE " + " AND ".join(conditions), params


def get_movies(conn, search_query="", public_view=False):
    where_clause, params = build_movie_search(search_query, public_view=public_view)
    return conn.execute(
        f"""
        SELECT
            m.*,
            (
                SELECT COUNT(*)
                FROM showtimes st
                WHERE st.movie_id = m.id
            ) AS schedule_count,
            (
                SELECT MIN(st.schedule_date)
                FROM showtimes st
                WHERE st.movie_id = m.id
            ) AS next_schedule_date
        FROM movies m
        {where_clause}
        ORDER BY COALESCE(m.created_at, '') DESC, m.id DESC
        """,
        params,
    ).fetchall()


def get_featured_movies(conn, month_value, public_view=False):
    query = """
        SELECT *
        FROM movies
        WHERE is_featured = 1 AND featured_month = ?
    """
    params = [month_value]

    if public_view:
        query += " AND COALESCE(status, '') != 'Archived'"

    query += " ORDER BY COALESCE(updated_at, created_at, '') DESC, id DESC"
    return conn.execute(query, params).fetchall()


def get_home_featured_movie(conn):
    featured_movie = conn.execute(
        """
        SELECT *
        FROM movies
        WHERE is_featured = 1
          AND featured_month = ?
          AND COALESCE(status, '') != 'Archived'
        ORDER BY COALESCE(updated_at, created_at, '') DESC, id DESC
        LIMIT 1
        """,
        (current_month_value(),),
    ).fetchone()

    if featured_movie:
        return featured_movie

    return conn.execute(
        """
        SELECT *
        FROM movies
        WHERE COALESCE(status, '') != 'Archived'
        ORDER BY COALESCE(created_at, '') DESC, id DESC
        LIMIT 1
        """
    ).fetchone()


def get_showtime_groups(conn, movie_id):
    showtime_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT id, movie_id, hall_name, schedule_date, schedule_time, created_at
            FROM showtimes
            WHERE movie_id = ?
            """,
            (movie_id,),
        ).fetchall()
    ]

    if not showtime_rows:
        return [], {}

    showtime_ids = [row["id"] for row in showtime_rows]
    placeholders = ",".join("?" for _ in showtime_ids)
    booked_seat_rows = conn.execute(
        f"""
        SELECT showtime_id, seat_label
        FROM booked_seats
        WHERE showtime_id IN ({placeholders})
        ORDER BY seat_label ASC
        """,
        showtime_ids,
    ).fetchall()

    booked_seat_map = defaultdict(list)
    for seat_row in booked_seat_rows:
        booked_seat_map[seat_row["showtime_id"]].append(seat_row["seat_label"])

    showtime_rows.sort(
        key=lambda row: (row["schedule_date"], get_time_sort_minutes(row["schedule_time"]), row["hall_name"] or "")
    )

    grouped_rows = []
    lookup = {}
    current_group = None

    for row in showtime_rows:
        booked_seats = booked_seat_map[row["id"]]
        row["booked_seats"] = booked_seats
        row["display_date"] = format_display_date(row["schedule_date"])
        row["display_time"] = format_display_time(row["schedule_time"])
        row["available_seats"] = TOTAL_SEAT_COUNT - len(booked_seats)
        row["occupancy_percent"] = round((len(booked_seats) / TOTAL_SEAT_COUNT) * 100)

        lookup[row["id"]] = {
            "id": row["id"],
            "date": row["display_date"],
            "time": row["display_time"],
            "hall_name": row["hall_name"] or DEFAULT_HALL_NAME,
            "booked_seats": booked_seats,
            "available_seats": row["available_seats"],
            "occupancy_percent": row["occupancy_percent"],
        }

        if current_group is None or current_group["date"] != row["schedule_date"]:
            current_group = {
                "date": row["schedule_date"],
                "display_date": row["display_date"],
                "entries": [],
            }
            grouped_rows.append(current_group)

        current_group["entries"].append(row)

    return grouped_rows, lookup


def get_admin_showtimes(conn):
    rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                st.id,
                st.movie_id,
                st.hall_name,
                st.schedule_date,
                st.schedule_time,
                st.created_at,
                m.title,
                m.cinema_name,
                (
                    SELECT COUNT(*)
                    FROM booked_seats bs
                    WHERE bs.showtime_id = st.id
                ) AS seats_booked
            FROM showtimes st
            JOIN movies m ON m.id = st.movie_id
            """
        ).fetchall()
    ]

    rows.sort(
        key=lambda row: (row["schedule_date"], get_time_sort_minutes(row["schedule_time"], invalid_minutes=-1), row["title"] or ""),
        reverse=True,
    )

    for row in rows:
        row["display_date"] = format_display_date(row["schedule_date"])
        row["display_time"] = format_display_time(row["schedule_time"])
        row["occupancy_percent"] = round((row["seats_booked"] / TOTAL_SEAT_COUNT) * 100)

    return rows


def get_recent_bookings(conn, limit=8, user_id=None):
    query = """
        SELECT
            b.id,
            b.booking_reference,
            b.user_id,
            b.movie_id,
            b.showtime_id,
            b.ticket_quantity,
            b.seats,
            b.payment_status,
            b.payment_provider,
            b.payment_amount,
            b.checkout_session_id,
            b.checkout_url,
            b.checkout_status,
            b.payment_reference,
            b.payment_paid_at,
            b.created_at,
            b.updated_at,
            u.name AS user_name,
            m.title,
            m.image,
            m.cinema_name,
            st.schedule_date,
            st.schedule_time,
            st.hall_name
        FROM bookings b
        JOIN users u ON u.id = b.user_id
        JOIN movies m ON m.id = b.movie_id
        JOIN showtimes st ON st.id = b.showtime_id
    """
    params = []

    if user_id is not None:
        query += " WHERE b.user_id = ?"
        params.append(user_id)

    query += " ORDER BY COALESCE(b.created_at, '') DESC, b.id DESC"
    if limit is not None:
        query += " LIMIT ?"
        params.append(limit)

    rows = [dict(row) for row in conn.execute(query, params).fetchall()]
    for row in rows:
        row["display_date"] = format_display_date(row["schedule_date"])
        row["display_time"] = format_display_time(row["schedule_time"])
        row["display_amount"] = format_php_amount(row["payment_amount"])
        row["seat_list"] = split_csv_values(row["seats"])
    return rows


def get_dashboard_stats(conn):
    return {
        "total_movies": conn.execute("SELECT COUNT(*) AS total FROM movies").fetchone()["total"],
        "active_users": conn.execute(
            "SELECT COUNT(*) AS total FROM users WHERE role = 'user'"
        ).fetchone()["total"],
        "featured_movies": conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM movies
            WHERE is_featured = 1 AND featured_month = ?
            """,
            (current_month_value(),),
        ).fetchone()["total"],
        "total_bookings": conn.execute("SELECT COUNT(*) AS total FROM bookings").fetchone()["total"],
        "today_bookings": conn.execute(
            "SELECT COUNT(*) AS total FROM bookings WHERE substr(created_at, 1, 10) = ?",
            (current_date_value(),),
        ).fetchone()["total"],
        "week_bookings": conn.execute(
            "SELECT COUNT(*) AS total FROM bookings WHERE substr(created_at, 1, 10) >= ?",
            ((datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d"),),
        ).fetchone()["total"],
        "total_transaction_value": conn.execute(
            """
            SELECT COALESCE(SUM(payment_amount), 0) AS total
            FROM bookings
            WHERE payment_status IN ('Pending', 'Paid')
            """
        ).fetchone()["total"],
        "paid_transaction_value": conn.execute(
            """
            SELECT COALESCE(SUM(payment_amount), 0) AS total
            FROM bookings
            WHERE payment_status = 'Paid'
            """
        ).fetchone()["total"],
        "pending_transaction_value": conn.execute(
            """
            SELECT COALESCE(SUM(payment_amount), 0) AS total
            FROM bookings
            WHERE payment_status = 'Pending'
            """
        ).fetchone()["total"],
    }


def get_report_data(conn):
    today_value = current_date_value()
    week_value = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")

    summary = {
        "daily_bookings": conn.execute(
            "SELECT COUNT(*) AS total FROM bookings WHERE substr(created_at, 1, 10) = ?",
            (today_value,),
        ).fetchone()["total"],
        "weekly_bookings": conn.execute(
            "SELECT COUNT(*) AS total FROM bookings WHERE substr(created_at, 1, 10) >= ?",
            (week_value,),
        ).fetchone()["total"],
        "paid_bookings": conn.execute(
            "SELECT COUNT(*) AS total FROM bookings WHERE payment_status = 'Paid'"
        ).fetchone()["total"],
        "pending_bookings": conn.execute(
            "SELECT COUNT(*) AS total FROM bookings WHERE payment_status = 'Pending'"
        ).fetchone()["total"],
        "total_transactions": conn.execute(
            """
            SELECT COUNT(*) AS total
            FROM bookings
            WHERE payment_status IN ('Pending', 'Paid')
            """
        ).fetchone()["total"],
        "total_transaction_value": conn.execute(
            """
            SELECT COALESCE(SUM(payment_amount), 0) AS total
            FROM bookings
            WHERE payment_status IN ('Pending', 'Paid')
            """
        ).fetchone()["total"],
        "paid_transaction_value": conn.execute(
            """
            SELECT COALESCE(SUM(payment_amount), 0) AS total
            FROM bookings
            WHERE payment_status = 'Paid'
            """
        ).fetchone()["total"],
        "pending_transaction_value": conn.execute(
            """
            SELECT COALESCE(SUM(payment_amount), 0) AS total
            FROM bookings
            WHERE payment_status = 'Pending'
            """
        ).fetchone()["total"],
    }

    top_movies = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                m.title,
                COUNT(b.id) AS booking_count,
                COALESCE(SUM(b.ticket_quantity), 0) AS tickets_sold
            FROM movies m
            LEFT JOIN bookings b
              ON b.movie_id = m.id
             AND b.payment_status IN ('Pending', 'Paid')
            GROUP BY m.id
            ORDER BY tickets_sold DESC, booking_count DESC, m.title ASC
            LIMIT 6
            """
        ).fetchall()
    ]

    featured_performance = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                m.title,
                COUNT(b.id) AS booking_count,
                COALESCE(SUM(b.ticket_quantity), 0) AS tickets_sold
            FROM movies m
            LEFT JOIN bookings b
              ON b.movie_id = m.id
             AND b.payment_status IN ('Pending', 'Paid')
            WHERE m.is_featured = 1
              AND m.featured_month = ?
            GROUP BY m.id
            ORDER BY tickets_sold DESC, booking_count DESC, m.title ASC
            """,
            (current_month_value(),),
        ).fetchall()
    ]

    occupancy_rows = [
        dict(row)
        for row in conn.execute(
            """
            SELECT
                st.id,
                st.hall_name,
                st.schedule_date,
                st.schedule_time,
                m.title,
                m.cinema_name,
                (
                    SELECT COUNT(*)
                    FROM booked_seats bs
                    WHERE bs.showtime_id = st.id
                ) AS seats_booked
            FROM showtimes st
            JOIN movies m ON m.id = st.movie_id
            ORDER BY st.schedule_date ASC, st.id ASC
            LIMIT 12
            """
        ).fetchall()
    ]

    for row in occupancy_rows:
        row["display_date"] = format_display_date(row["schedule_date"])
        row["display_time"] = format_display_time(row["schedule_time"])
        row["occupancy_percent"] = round((row["seats_booked"] / TOTAL_SEAT_COUNT) * 100)

    average_occupancy = 0
    if occupancy_rows:
        average_occupancy = round(
            sum(row["occupancy_percent"] for row in occupancy_rows) / len(occupancy_rows)
        )

    summary["average_occupancy"] = average_occupancy

    return {
        "summary": summary,
        "top_movies": top_movies,
        "featured_performance": featured_performance,
        "occupancy_rows": occupancy_rows,
        "recent_bookings": get_recent_bookings(conn, limit=12),
    }


init_db()


@app.context_processor
def inject_global_template_data():
    return {
        "current_month_label": format_month_label(current_month_value()),
        "current_month_value": current_month_value(),
        "format_month_label": format_month_label,
        "format_display_date": format_display_date,
        "format_display_time": format_display_time,
        "format_php_amount": format_php_amount,
        "payment_statuses": PAYMENT_STATUSES,
        "seat_labels": ALL_SEAT_LABELS,
        "total_seat_count": TOTAL_SEAT_COUNT,
    }


@app.route("/")
def home():
    if "role" in session and session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))

    conn = get_db_connection()
    featured_movie = get_home_featured_movie(conn)
    spotlight_movies = get_movies(conn, public_view=True)[:4]
    featured_movies = get_featured_movies(conn, current_month_value(), public_view=True)
    conn.close()

    return render_template(
        "index.html",
        name=session.get("user_name"),
        featured_movie=featured_movie,
        spotlight_movies=spotlight_movies,
        featured_movies=featured_movies,
    )


@app.route("/movies")
def movies_page():
    if "user_id" not in session:
        return redirect(url_for("signin_page"))

    search_query = (request.args.get("q") or "").strip()
    conn = get_db_connection()
    movies = get_movies(conn, search_query, public_view=True)
    featured_movies = get_featured_movies(conn, current_month_value(), public_view=True)
    conn.close()

    return render_template(
        "movies.html",
        name=session.get("user_name"),
        movies=movies,
        featured_movies=featured_movies,
        search_query=search_query,
    )


@app.route("/categories")
def categories():
    if "user_id" not in session:
        return redirect(url_for("signin_page"))

    conn = get_db_connection()
    movies = get_movies(conn, public_view=True)
    conn.close()

    return render_template("categories.html", name=session.get("user_name"), movies=movies)


@app.route("/movie/<int:movie_id>")
def movie_details(movie_id):
    if "user_id" not in session:
        return redirect(url_for("signin_page"))

    conn = get_db_connection()
    movie = conn.execute("SELECT * FROM movies WHERE id = ?", (movie_id,)).fetchone()
    if movie is None:
        conn.close()
        return "Movie not found", 404

    showtime_groups, showtime_lookup = get_showtime_groups(conn, movie_id)
    related_movies = conn.execute(
        """
        SELECT id, title, image, status
        FROM movies
        WHERE id != ?
          AND COALESCE(status, '') != 'Archived'
        ORDER BY COALESCE(created_at, '') DESC, id DESC
        LIMIT 3
        """,
        (movie_id,),
    ).fetchall()
    conn.close()
    ticket_price_display = None

    try:
        ticket_price_display = format_php_amount(get_ticket_price_centavos())
    except ValueError:
        ticket_price_display = None

    return render_template(
        "movie_details.html",
        name=session.get("user_name"),
        movie=movie,
        showtime_groups=showtime_groups,
        showtime_lookup=showtime_lookup,
        related_movies=related_movies,
        ticket_price_display=ticket_price_display,
    )


@app.route("/my-bookings")
def booking_history_page():
    if "user_id" not in session:
        return redirect(url_for("signin_page"))

    conn = get_db_connection()
    bookings = get_recent_bookings(conn, limit=None, user_id=session["user_id"])
    conn.close()

    active_count = sum(1 for booking in bookings if booking["payment_status"] in ("Pending", "Paid"))
    paid_count = sum(1 for booking in bookings if booking["payment_status"] == "Paid")
    cancelled_count = sum(1 for booking in bookings if booking["payment_status"] in ("Cancelled", "Refunded"))

    return render_template(
        "booking_history.html",
        name=session.get("user_name"),
        bookings=bookings,
        booking_summary={
            "total": len(bookings),
            "active": active_count,
            "paid": paid_count,
            "cancelled": cancelled_count,
        },
    )


@app.route("/about")
def about():
    return render_template("about.html", name=session.get("user_name"))


@app.route("/signin")
def signin_page():
    return render_template("signin.html")


@app.route("/signup")
def signup_page():
    return render_template("signup.html")


@app.route("/login_process", methods=["POST"])
def login_process():
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""

    conn = get_db_connection()
    user = conn.execute(
        "SELECT * FROM users WHERE email = ? AND password = ?",
        (email, password),
    ).fetchone()
    conn.close()

    if user:
        session["user_id"] = user["id"]
        session["user_name"] = user["name"]
        session["role"] = user["role"]

        if user["role"] == "admin":
            return redirect(url_for("admin_dashboard"))
        return redirect(url_for("home"))

    flash("Invalid email or password.", "error")
    return redirect(url_for("signin_page"))


@app.route("/signup_process", methods=["POST"])
def signup_process():
    name = (request.form.get("name") or "").strip()
    email = (request.form.get("email") or "").strip()
    password = request.form.get("password") or ""
    confirm_password = request.form.get("confirm_password") or ""

    if not name or not email or not password:
        flash("Please complete all sign up fields.", "error")
        return redirect(url_for("signup_page"))

    if password != confirm_password:
        flash("Password confirmation does not match.", "error")
        return redirect(url_for("signup_page"))

    try:
        conn = get_db_connection()
        conn.execute(
            "INSERT INTO users (name, email, password, role) VALUES (?, ?, ?, ?)",
            (name, email, password, "user"),
        )
        conn.commit()
        conn.close()
        flash("Account created successfully. You can sign in now.", "success")
        return redirect(url_for("signin_page"))
    except sqlite3.IntegrityError:
        flash("Email already exists.", "error")
        return redirect(url_for("signup_page"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


@app.route("/bookings/create", methods=["POST"])
def create_booking():
    if "user_id" not in session:
        return redirect(url_for("signin_page"))

    showtime_id = safe_int(request.form.get("showtime_id"))
    ticket_quantity = safe_int(request.form.get("ticket_quantity"))
    selected_seats = [seat.upper() for seat in split_csv_values(request.form.get("selected_seats"))]
    selected_seats = list(dict.fromkeys(selected_seats))

    if not showtime_id or not ticket_quantity or ticket_quantity < 1:
        flash("Please choose a valid showtime and ticket quantity.", "error")
        return redirect(request.referrer or url_for("movies_page"))

    if ticket_quantity > 6:
        flash("Maximum booking per transaction is 6 tickets.", "error")
        return redirect(request.referrer or url_for("movies_page"))

    if len(selected_seats) != ticket_quantity:
        flash("Selected seats must match the ticket quantity.", "error")
        return redirect(request.referrer or url_for("movies_page"))

    invalid_seats = [seat for seat in selected_seats if seat not in ALL_SEAT_LABELS]
    if invalid_seats:
        flash("One or more selected seats are invalid.", "error")
        return redirect(request.referrer or url_for("movies_page"))

    try:
        get_paymongo_secret_key()
        get_paymongo_payment_method_types()
        ticket_price_centavos = get_ticket_price_centavos()
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(request.referrer or url_for("movies_page"))

    conn = get_db_connection()
    showtime = conn.execute(
        """
        SELECT
            st.id,
            st.hall_name,
            st.schedule_date,
            st.schedule_time,
            m.id AS movie_id,
            m.title,
            m.cinema_name,
            m.status
        FROM showtimes st
        JOIN movies m ON m.id = st.movie_id
        WHERE st.id = ?
        """,
        (showtime_id,),
    ).fetchone()

    if showtime is None:
        conn.close()
        flash("Selected showtime was not found.", "error")
        return redirect(request.referrer or url_for("movies_page"))

    if showtime["status"] != "Now Showing":
        conn.close()
        flash("Bookings are only available for Now Showing movies.", "error")
        return redirect(url_for("movie_details", movie_id=showtime["movie_id"]))

    user_row = conn.execute(
        "SELECT id, name, email FROM users WHERE id = ?",
        (session["user_id"],),
    ).fetchone()

    taken_seats = {
        row["seat_label"]
        for row in conn.execute(
            "SELECT seat_label FROM booked_seats WHERE showtime_id = ?",
            (showtime_id,),
        ).fetchall()
    }

    for seat_label in selected_seats:
        if seat_label in taken_seats:
            conn.close()
            flash(f"Seat {seat_label} has already been booked. Please choose another seat.", "error")
            return redirect(url_for("movie_details", movie_id=showtime["movie_id"]))

    timestamp = current_timestamp()
    checkout_url = None
    total_amount = ticket_price_centavos * ticket_quantity
    try:
        cursor = conn.execute(
            """
            INSERT INTO bookings (
                booking_reference,
                user_id,
                movie_id,
                showtime_id,
                ticket_quantity,
                seats,
                payment_status,
                payment_provider,
                payment_amount,
                checkout_status,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "",
                session["user_id"],
                showtime["movie_id"],
                showtime_id,
                ticket_quantity,
                ", ".join(selected_seats),
                "Pending",
                "PayMongo",
                total_amount,
                "pending",
                timestamp,
                timestamp,
            ),
        )
        booking_id = cursor.lastrowid
        booking_reference = build_booking_reference(booking_id)
        conn.execute(
            "UPDATE bookings SET booking_reference = ? WHERE id = ?",
            (booking_reference, booking_id),
        )

        for seat_label in selected_seats:
            conn.execute(
                """
                INSERT INTO booked_seats (booking_id, showtime_id, seat_label, created_at)
                VALUES (?, ?, ?, ?)
                """,
                (booking_id, showtime_id, seat_label, timestamp),
            )

        booking_row = {
            "id": booking_id,
            "booking_reference": booking_reference,
            "payment_amount": total_amount,
            "ticket_quantity": ticket_quantity,
            "seats": ", ".join(selected_seats),
            "title": showtime["title"],
            "cinema_name": showtime["cinema_name"],
            "schedule_date": showtime["schedule_date"],
            "schedule_time": showtime["schedule_time"],
            "hall_name": showtime["hall_name"],
            "user_name": user_row["name"] if user_row else "",
            "user_email": user_row["email"] if user_row else "",
            "checkout_session_id": None,
            "checkout_url": None,
            "payment_reference": None,
        }
        checkout_result = ensure_booking_checkout_session(conn, booking_row)
        checkout_url = checkout_result["checkout_url"]

        conn.commit()
    except sqlite3.IntegrityError:
        conn.rollback()
        conn.close()
        flash("Some of the selected seats were just booked by another user. Please try again.", "error")
        return redirect(url_for("movie_details", movie_id=showtime["movie_id"]))
    except (RuntimeError, ValueError) as exc:
        conn.rollback()
        conn.close()
        flash(f"Could not start PayMongo checkout: {exc}", "error")
        return redirect(url_for("movie_details", movie_id=showtime["movie_id"]))

    conn.close()
    if not checkout_url:
        flash("Booking was created, but PayMongo did not return a checkout URL.", "error")
        return redirect(url_for("booking_history_page"))

    return redirect(checkout_url)


@app.route("/bookings/pay/<int:booking_id>", methods=["POST"])
def start_booking_checkout(booking_id):
    if "user_id" not in session:
        return redirect(url_for("signin_page"))

    try:
        get_paymongo_secret_key()
        get_paymongo_payment_method_types()
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("booking_history_page"))

    conn = get_db_connection()
    booking = get_booking_checkout_context(conn, booking_id, session["user_id"])

    if booking is None:
        conn.close()
        flash("Booking not found.", "error")
        return redirect(url_for("booking_history_page"))

    if booking["payment_status"] in ("Cancelled", "Refunded"):
        conn.close()
        flash("Closed bookings can no longer be paid.", "error")
        return redirect(url_for("booking_history_page"))

    try:
        checkout_result = ensure_booking_checkout_session(conn, booking)
        conn.commit()
    except (RuntimeError, ValueError) as exc:
        conn.rollback()
        conn.close()
        flash(f"Could not open PayMongo checkout: {exc}", "error")
        return redirect(url_for("booking_history_page"))

    conn.close()

    if checkout_result["is_paid"]:
        flash("This booking has already been paid.", "success")
        return redirect(url_for("booking_history_page"))

    if not checkout_result["checkout_url"]:
        flash("PayMongo did not return a checkout URL for this booking.", "error")
        return redirect(url_for("booking_history_page"))

    return redirect(checkout_result["checkout_url"])


@app.route("/payments/paymongo/success/<int:booking_id>")
def paymongo_checkout_success(booking_id):
    conn = get_db_connection()
    booking = get_booking_checkout_context(conn, booking_id)

    if booking is None:
        conn.close()
        flash("Booking not found.", "error")
        if "user_id" in session:
            return redirect(url_for("booking_history_page"))
        return redirect(url_for("signin_page"))

    if not booking["checkout_session_id"]:
        conn.close()
        flash("This booking does not have a PayMongo checkout session yet.", "error")
        if "user_id" in session:
            return redirect(url_for("booking_history_page"))
        return redirect(url_for("signin_page"))

    try:
        checkout_payload = retrieve_paymongo_checkout_session(booking["checkout_session_id"])
        sync_result = sync_booking_with_checkout_session(conn, booking_id, checkout_payload)
        conn.commit()
    except RuntimeError as exc:
        conn.rollback()
        conn.close()
        flash(f"Could not confirm the PayMongo payment yet: {exc}", "error")
        if "user_id" in session:
            return redirect(url_for("booking_history_page"))
        return redirect(url_for("signin_page"))

    conn.close()

    if sync_result["is_paid"]:
        flash("Payment completed successfully via PayMongo.", "success")
    else:
        flash("Checkout returned, but payment is still pending. You can continue from My Bookings.", "error")

    if "user_id" in session:
        return redirect(url_for("booking_history_page"))
    return redirect(url_for("signin_page"))


@app.route("/payments/paymongo/cancel/<int:booking_id>")
def paymongo_checkout_cancel(booking_id):
    if "user_id" not in session:
        flash("PayMongo checkout was cancelled.", "error")
        return redirect(url_for("signin_page"))

    flash("PayMongo checkout was cancelled. Your booking is still pending, and you can continue payment anytime.", "error")
    return redirect(url_for("booking_history_page"))


@app.route("/webhooks/paymongo", methods=["POST"])
def paymongo_webhook():
    raw_payload = request.get_data()
    signature_header = request.headers.get("Paymongo-Signature") or request.headers.get("paymongo-signature") or ""

    is_valid_signature = verify_paymongo_webhook_signature(signature_header, raw_payload)
    if is_valid_signature is None:
        return jsonify({"message": "Webhook handling is disabled. Checkout uses PayMongo secret key only."}), 200

    if not is_valid_signature:
        return jsonify({"message": "Invalid PayMongo signature."}), 401

    try:
        payload = json.loads(raw_payload.decode("utf-8"))
    except json.JSONDecodeError:
        return jsonify({"message": "Invalid JSON payload."}), 400

    event_data = payload.get("data") or {}
    event_attributes = event_data.get("attributes") or {}
    event_type = event_attributes.get("type") or ""
    resource_data = event_attributes.get("data") or {}

    conn = get_db_connection()
    try:
        if event_type == "checkout_session.payment.paid":
            booking_id = resolve_booking_id_for_checkout_session(conn, resource_data)
            if booking_id is not None:
                sync_booking_with_checkout_session(conn, booking_id, {"data": resource_data})
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise

    conn.close()
    return jsonify({"message": "SUCCESS"}), 200


@app.route("/bookings/cancel/<int:booking_id>", methods=["POST"])
def cancel_booking(booking_id):
    if "user_id" not in session:
        return redirect(url_for("signin_page"))

    conn = get_db_connection()
    booking = conn.execute(
        """
        SELECT id, payment_status, checkout_session_id, checkout_status
        FROM bookings
        WHERE id = ? AND user_id = ?
        """,
        (booking_id, session["user_id"]),
    ).fetchone()

    if booking is None:
        conn.close()
        flash("Booking not found.", "error")
        return redirect(url_for("booking_history_page"))

    if booking["payment_status"] in ("Cancelled", "Refunded"):
        conn.close()
        flash("This booking is already closed.", "error")
        return redirect(url_for("booking_history_page"))

    if booking["checkout_session_id"] and booking["checkout_status"] == "active":
        try:
            expire_paymongo_checkout_session(booking["checkout_session_id"])
        except RuntimeError:
            pass

    release_booking_seats(conn, booking_id)
    conn.execute(
        """
        UPDATE bookings
        SET payment_status = 'Cancelled',
            checkout_status = CASE
                WHEN checkout_status = 'active' THEN 'expired'
                ELSE checkout_status
            END,
            updated_at = ?
        WHERE id = ?
        """,
        (current_timestamp(), booking_id),
    )
    conn.commit()
    conn.close()

    flash("Booking cancelled successfully. Your seats are available again.", "success")
    return redirect(url_for("booking_history_page"))


@app.route("/admin")
def admin_dashboard():
    if session.get("role") != "admin":
        return "Access Denied!", 403

    conn = get_db_connection()
    movies = get_movies(conn)
    showtime_rows = get_admin_showtimes(conn)
    all_movies = conn.execute(
        "SELECT id, title FROM movies ORDER BY LOWER(title) ASC"
    ).fetchall()
    stats = get_dashboard_stats(conn)
    recent_bookings = get_recent_bookings(conn, limit=6)
    conn.close()

    return render_template(
        "admindashboard.html",
        name=session.get("user_name"),
        movies=movies,
        recent_movies=movies[:5],
        stats=stats,
        showtime_rows=showtime_rows,
        all_movies=all_movies,
        recent_bookings=recent_bookings,
    )


@app.route("/admin/movies")
def admin_movies():
    if session.get("role") != "admin":
        return redirect(url_for("signin_page"))

    search_query = (request.args.get("q") or "").strip()
    conn = get_db_connection()
    movies = get_movies(conn, search_query)
    conn.close()

    return render_template(
        "admin_movies.html",
        name=session.get("user_name"),
        movies=movies,
        search_query=search_query,
    )


@app.route("/admin/reports")
def admin_reports():
    if session.get("role") != "admin":
        return redirect(url_for("signin_page"))

    conn = get_db_connection()
    report_data = get_report_data(conn)
    conn.close()

    return render_template(
        "admin_reports.html",
        name=session.get("user_name"),
        report_data=report_data,
    )


@app.route("/admin/bookings/<int:booking_id>/payment", methods=["POST"])
def update_booking_payment(booking_id):
    if session.get("role") != "admin":
        return redirect(url_for("signin_page"))

    new_status = (request.form.get("payment_status") or "").strip()
    if new_status not in PAYMENT_STATUSES:
        flash("Invalid payment status selected.", "error")
        return redirect(request.referrer or url_for("admin_reports"))

    conn = get_db_connection()
    booking = conn.execute(
        """
        SELECT id, payment_status
        FROM bookings
        WHERE id = ?
        """,
        (booking_id,),
    ).fetchone()

    if booking is None:
        conn.close()
        flash("Booking not found.", "error")
        return redirect(request.referrer or url_for("admin_reports"))

    if booking["payment_status"] in ("Cancelled", "Refunded") and new_status in ("Pending", "Paid"):
        has_seats = conn.execute(
            "SELECT COUNT(*) AS total FROM booked_seats WHERE booking_id = ?",
            (booking_id,),
        ).fetchone()["total"]
        if has_seats == 0:
            conn.close()
            flash("Cancelled or refunded bookings cannot return to active status after seats were released.", "error")
            return redirect(request.referrer or url_for("admin_reports"))

    if new_status in ("Cancelled", "Refunded"):
        release_booking_seats(conn, booking_id)

    conn.execute(
        """
        UPDATE bookings
        SET payment_status = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (new_status, current_timestamp(), booking_id),
    )
    conn.commit()
    conn.close()

    flash("Booking payment status updated successfully.", "success")
    return redirect(request.referrer or url_for("admin_reports"))


@app.route("/add_movie", methods=["POST"])
def add_movie():
    if session.get("role") != "admin":
        return redirect(url_for("signin_page"))

    image_file = request.files.get("image_file")
    conn = get_db_connection()
    payload = normalize_movie_payload(request.form)
    errors = validate_movie_payload(conn, payload, image_file=image_file)

    if not errors and payload["show_date"] and payload["showtime_list"]:
        errors.extend(
            validate_showtime_conflicts(
                conn,
                payload["cinema"],
                payload["hall_name"],
                payload["show_date"],
                payload["showtime_list"],
                payload["runtime_minutes"],
            )
        )

    if errors:
        conn.close()
        for error in errors:
            flash(error, "error")
        return redirect(url_for("admin_dashboard"))

    image_db_path = save_image_file(image_file)
    timestamp = current_timestamp()
    cursor = conn.execute(
        """
        INSERT INTO movies (
            title,
            image,
            status,
            genre,
            trailer_link,
            description,
            cinema_name,
            hall_name,
            showtimes,
            show_date,
            actors,
            directors,
            is_featured,
            featured_month,
            runtime_minutes,
            created_at,
            updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            payload["title"],
            image_db_path,
            payload["status"],
            payload["genre"],
            payload["trailer"],
            payload["description"],
            payload["cinema"],
            payload["hall_name"],
            payload["showtimes_text"],
            payload["show_date"] or None,
            payload["actors"],
            payload["directors"],
            payload["is_featured"],
            payload["featured_month"],
            payload["runtime_minutes"],
            timestamp,
            timestamp,
        ),
    )
    movie_id = cursor.lastrowid

    if payload["show_date"] and payload["showtime_list"]:
        replace_movie_showtimes(
            conn,
            movie_id,
            payload["hall_name"],
            payload["show_date"],
            payload["showtime_list"],
        )

    conn.commit()
    conn.close()

    flash(f'"{payload["title"]}" added successfully.', "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/delete_movie/<int:id>")
def delete_movie(id):
    if session.get("role") != "admin":
        return redirect(url_for("signin_page"))

    conn = get_db_connection()
    movie = conn.execute("SELECT title FROM movies WHERE id = ?", (id,)).fetchone()
    existing_bookings = conn.execute(
        "SELECT COUNT(*) AS total FROM bookings WHERE movie_id = ?",
        (id,),
    ).fetchone()["total"]

    if existing_bookings:
        conn.close()
        flash("Cannot delete a movie that already has booking records.", "error")
        return redirect(url_for("admin_movies"))

    conn.execute("DELETE FROM movies WHERE id = ?", (id,))
    conn.commit()
    conn.close()

    if movie:
        flash(f'"{movie["title"]}" deleted successfully.', "success")
    return redirect(url_for("admin_movies"))


@app.route("/edit_movie/<int:id>", methods=["POST"])
def edit_movie(id):
    if session.get("role") != "admin":
        return redirect(url_for("signin_page"))

    image_file = request.files.get("image_file")
    conn = get_db_connection()
    movie = conn.execute("SELECT * FROM movies WHERE id = ?", (id,)).fetchone()
    if movie is None:
        conn.close()
        flash("Movie not found.", "error")
        return redirect(url_for("admin_movies"))

    payload = normalize_movie_payload(request.form)
    errors = validate_movie_payload(conn, payload, image_file=image_file, movie_id=id)
    schedule_changed = (
        (payload["show_date"] or None) != (movie["show_date"] or None)
        or payload["showtimes_text"] != (movie["showtimes"] or "")
        or payload["hall_name"] != (movie["hall_name"] or DEFAULT_HALL_NAME)
    )
    has_existing_bookings = conn.execute(
        "SELECT COUNT(*) AS total FROM bookings WHERE movie_id = ?",
        (id,),
    ).fetchone()["total"] > 0

    if has_existing_bookings and schedule_changed:
        errors.append(
            "This movie already has bookings. Keep the current schedule intact and add future showtimes from the admin schedule panel."
        )

    if not errors and payload["show_date"] and payload["showtime_list"]:
        errors.extend(
            validate_showtime_conflicts(
                conn,
                payload["cinema"],
                payload["hall_name"],
                payload["show_date"],
                payload["showtime_list"],
                payload["runtime_minutes"],
                exclude_movie_id=id,
            )
        )

    if errors:
        conn.close()
        for error in errors:
            flash(error, "error")
        return redirect(url_for("edit_movie_page", id=id))

    image_path = movie["image"]
    if image_file and image_file.filename:
        image_path = save_image_file(image_file)

    conn.execute(
        """
        UPDATE movies
        SET title = ?,
            genre = ?,
            status = ?,
            trailer_link = ?,
            description = ?,
            cinema_name = ?,
            hall_name = ?,
            showtimes = ?,
            show_date = ?,
            image = ?,
            actors = ?,
            directors = ?,
            is_featured = ?,
            featured_month = ?,
            runtime_minutes = ?,
            updated_at = ?
        WHERE id = ?
        """,
        (
            payload["title"],
            payload["genre"],
            payload["status"],
            payload["trailer"],
            payload["description"],
            payload["cinema"],
            payload["hall_name"],
            payload["showtimes_text"],
            payload["show_date"] or None,
            image_path,
            payload["actors"],
            payload["directors"],
            payload["is_featured"],
            payload["featured_month"],
            payload["runtime_minutes"],
            current_timestamp(),
            id,
        ),
    )
    if schedule_changed:
        replace_movie_showtimes(
            conn,
            id,
            payload["hall_name"],
            payload["show_date"],
            payload["showtime_list"],
        )
    conn.commit()
    conn.close()

    flash(f'DBMS update: "{payload["title"]}" updated successfully.', "success")
    return redirect(url_for("admin_movies"))


@app.route("/edit_movie_page/<int:id>")
def edit_movie_page(id):
    if session.get("role") != "admin":
        return redirect(url_for("signin_page"))

    conn = get_db_connection()
    movie = conn.execute("SELECT * FROM movies WHERE id = ?", (id,)).fetchone()
    showtime_groups, _ = get_showtime_groups(conn, id)
    conn.close()

    if movie is None:
        flash("Movie not found.", "error")
        return redirect(url_for("admin_movies"))

    return render_template(
        "edit_movie.html",
        movie=movie,
        name=session.get("user_name"),
        showtime_groups=showtime_groups,
    )


@app.route("/admin/showtimes/add", methods=["POST"])
def add_showtime():
    if session.get("role") != "admin":
        return redirect(url_for("signin_page"))

    movie_id = safe_int(request.form.get("movie_id"))
    hall_name = (request.form.get("hall_name") or "").strip()
    schedule_date = (request.form.get("schedule_date") or "").strip()
    schedule_times, invalid_times = normalize_schedule_values(request.form.get("schedule_times"))

    if not movie_id or not hall_name or not schedule_date or not schedule_times or invalid_times:
        flash("Please choose a movie, hall, valid date, and at least one valid showtime.", "error")
        return redirect(url_for("admin_dashboard"))

    conn = get_db_connection()
    movie = conn.execute(
        "SELECT id, title, cinema_name, runtime_minutes FROM movies WHERE id = ?",
        (movie_id,),
    ).fetchone()
    if movie is None:
        conn.close()
        flash("Selected movie was not found.", "error")
        return redirect(url_for("admin_dashboard"))

    conflict_errors = validate_showtime_conflicts(
        conn,
        movie["cinema_name"],
        hall_name,
        schedule_date,
        schedule_times,
        movie["runtime_minutes"] or DEFAULT_RUNTIME_MINUTES,
    )
    if conflict_errors:
        conn.close()
        for error in conflict_errors:
            flash(error, "error")
        return redirect(url_for("admin_dashboard"))

    add_movie_showtimes(conn, movie["id"], hall_name, schedule_date, schedule_times)
    conn.commit()
    conn.close()

    flash(f'Showtimes added for "{movie["title"]}" in {hall_name}.', "success")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/showtimes/delete/<int:showtime_id>")
def delete_showtime(showtime_id):
    if session.get("role") != "admin":
        return redirect(url_for("signin_page"))

    conn = get_db_connection()
    showtime = conn.execute(
        """
        SELECT st.id, st.movie_id
        FROM showtimes st
        WHERE st.id = ?
        """,
        (showtime_id,),
    ).fetchone()

    if showtime is None:
        conn.close()
        flash("Showtime not found.", "error")
        return redirect(url_for("admin_dashboard"))

    existing_bookings = conn.execute(
        "SELECT COUNT(*) AS total FROM bookings WHERE showtime_id = ?",
        (showtime_id,),
    ).fetchone()["total"]
    if existing_bookings:
        conn.close()
        flash("Cannot delete a showtime that already has bookings.", "error")
        return redirect(url_for("admin_dashboard"))

    conn.execute("DELETE FROM showtimes WHERE id = ?", (showtime_id,))
    sync_movie_schedule_summary(conn, showtime["movie_id"])
    conn.commit()
    conn.close()

    flash("Showtime deleted successfully.", "success")
    return redirect(url_for("admin_dashboard"))


if __name__ == "__main__":
    app.run(debug=True)
