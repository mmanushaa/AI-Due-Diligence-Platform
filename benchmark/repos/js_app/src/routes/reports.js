"use strict";

const express = require("express");
const { requireAuth } = require("../auth/requireAuth");

const router = express.Router();

router.use(requireAuth);

router.get("/monthly", (req, res) => {
  res.json({ report: "monthly", owner: req.userId });
});

module.exports = router;
