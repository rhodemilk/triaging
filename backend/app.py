from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route("/api/test", methods=["POST"])
def test():
    data = request.get_json()

    print("Received:", data)

    return jsonify({
        "status": "success",
        "received": data
    })

app.run(host="0.0.0.0", port=5001)