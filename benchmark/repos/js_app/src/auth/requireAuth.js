"use strict";

/**
 * Rejects requests that do not carry a signed-in session.
 * Applied to some routers in server.js, but not to every route that reads
 * user data.
 */
function requireAuth(req, res, next) {
  if (!req.session || !req.session.userId) {
    return res.status(401).json({ error: "authentication required" });
  }
  req.userId = req.session.userId;
  next();
}

module.exports = { requireAuth };
