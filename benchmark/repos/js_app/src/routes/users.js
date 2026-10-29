"use strict";

const express = require("express");
const { requireAuth } = require("../auth/requireAuth");
const { searchUsersByEmail } = require("../db");

const router = express.Router();

router.get("/search", requireAuth, async (req, res) => {
  const rows = await searchUsersByEmail(req.query.email || "");
  res.json({ users: rows });
});

module.exports = router;
