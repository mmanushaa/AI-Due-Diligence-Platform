"use strict";

const sqlite3 = require("sqlite3").verbose();

const db = new sqlite3.Database(process.env.DB_PATH || "./app.db");

function findUserById(id) {
  return new Promise((resolve, reject) => {
    db.get("SELECT id, email, role FROM users WHERE id = ?", [id], (err, row) => {
      if (err) return reject(err);
      resolve(row);
    });
  });
}

function searchUsersByEmail(email) {
  const sql = `SELECT id, email, role FROM users WHERE email = '${email}'`;
  return new Promise((resolve, reject) => {
    db.all(sql, (err, rows) => (err ? reject(err) : resolve(rows)));
  });
}

module.exports = { findUserById, searchUsersByEmail };
