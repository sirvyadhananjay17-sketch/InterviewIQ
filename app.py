import os
import json
import PyPDF2
from flask import Flask, render_template, request, redirect, url_for, flash
import mysql.connector
import google.generativeai as genai
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = "interviewiq_secret_key"
app.config['UPLOAD_FOLDER'] = 'uploads'

# Configure Google Gemini API
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Ensure upload directory exists
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)


def get_db_connection():
    """Establishes connection to MySQL server."""
    return mysql.connector.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        user=os.getenv("MYSQL_USER", "root"),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "interviewiq_db")
    )


def extract_text_from_pdf(pdf_path):
    """Extracts raw text content from an uploaded PDF file using PyPDF2."""
    text = ""
    with open(pdf_path, 'rb') as file:
        reader = PyPDF2.PdfReader(file)
        for page in reader.pages:
            extracted = page.extract_text()
            if extracted:
                text += extracted + "\n"
    return text


@app.route('/')
def index():
    """Home page route with resume upload form."""
    return render_template('index.html')


@app.route('/process-resume', methods=['POST'])
def process_resume():
    """Handles PDF file upload, text extraction, and question generation."""
    name = request.form.get('name')
    email = request.form.get('email')
    file = request.files.get('resume')

    if not file or file.filename == '':
        flash("Please select a valid PDF resume file.")
        return redirect(url_for('index'))

    # Save PDF locally
    filepath = os.path.join(app.config['UPLOAD_FOLDER'], file.filename)
    file.save(filepath)

    # 1. Extract text from PDF
    resume_text = extract_text_from_pdf(filepath)

    # 2. Insert candidate record into MySQL database
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO candidates (name, email) VALUES (%s, %s)",
        (name, email)
    )
    conn.commit()
    candidate_id = cursor.lastrowid

    # 3. Request tailored interview questions from Gemini API
    model = genai.GenerativeModel('gemini-1.5-flash')
    prompt = f"""
    Act as a technical interviewer. Read the following resume text and generate 3 tailored interview questions.
    Return ONLY a JSON array of strings containing the questions, without markdown wrapping or prose.
    Resume Text:
    {resume_text[:2000]}
    """
    
    try:
        response = model.generate_content(prompt)
        cleaned_json = response.text.strip().replace("```json", "").replace("```", "")
        questions = json.loads(cleaned_json)
    except Exception as e:
        print(f"Error parsing Gemini response: {e}")
        questions = [
            "Walk me through your key technical projects listed on your resume.",
            "What technical challenge did you face in your last project, and how did you resolve it?",
            "What core technical skills do you feel most confident in, and why?"
        ]

    # Save session placeholder in database
    cursor.execute(
        "INSERT INTO interview_sessions (candidate_id, questions) VALUES (%s, %s)",
        (candidate_id, json.dumps(questions))
    )
    conn.commit()
    session_id = cursor.lastrowid

    cursor.close()
    conn.close()

    # Clean up uploaded local file
    if os.path.exists(filepath):
        os.remove(filepath)

    return redirect(url_for('interview', session_id=session_id))


@app.route('/interview/<int:session_id>')
def interview(session_id):
    """Displays the interactive mock interview room."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM interview_sessions WHERE id = %s", (session_id,))
    session_data = cursor.fetchone()
    cursor.close()
    conn.close()

    if not session_data:
        flash("Interview session not found.")
        return redirect(url_for('index'))

    questions = json.loads(session_data['questions'])
    return render_template('interview.html', session_id=session_id, questions=questions)


@app.route('/evaluate-interview/<int:session_id>', methods=['POST'])
def evaluate_interview(session_id):
    """Evaluates user responses using Gemini and updates MySQL."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM interview_sessions WHERE id = %s", (session_id,))
    session_data = cursor.fetchone()

    questions = json.loads(session_data['questions'])
    user_answers = [request.form.get(f"answer_{i}", "") for i in range(len(questions))]

    # Build evaluation prompt for Gemini
    qa_pairs = ""
    for q, a in zip(questions, user_answers):
        qa_pairs += f"Question: {q}\nCandidate Answer: {a}\n---\n"

    model = genai.GenerativeModel('gemini-1.5-flash')
    prompt = f"""
    Evaluate these interview responses:
    {qa_pairs}

    Provide:
    1. Overall score out of 100.
    2. Detailed feedback with strengths and areas of improvement.

    Return the output ONLY as JSON in this exact structure:
    {{
        "score": 85,
        "feedback": "Detailed critique text here..."
    }}
    """

    try:
        response = model.generate_content(prompt)
        cleaned_json = response.text.strip().replace("```json", "").replace("```", "")
        evaluation = json.loads(cleaned_json)
        score = evaluation.get("score", 70)
        feedback = evaluation.get("feedback", "Completed successfully.")
    except Exception as e:
        print(f"Error evaluating response: {e}")
        score = 75
        feedback = "Response submitted successfully. General rating applied."

    # Update session results in DB
    cursor.execute(
        "UPDATE interview_sessions SET score = %s, feedback = %s WHERE id = %s",
        (score, feedback, session_id)
    )
    conn.commit()
    cursor.close()
    conn.close()

    return render_template('results.html', score=score, feedback=feedback)


if __name__ == '__main__':
    app.run(debug=True)