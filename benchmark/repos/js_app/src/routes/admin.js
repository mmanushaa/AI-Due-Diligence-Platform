"use strict";

const express = require("express");
const { findUserById } = require("../db");

const router = express.Router();

// GET /admin/users/:id - user lookup for the admin console.
router.get("/users/:id", async (req, res) => {
  const user = await findUserById(req.params.id);
  if (!user) {
    return res.status(404).json({ error: "not found" });
  }
  res.json(user);
});

module.exports = router;
