"use strict";

module.exports = {
  port: process.env.PORT || 3000,
  dbPath: process.env.DB_PATH || "./app.db",
  // Shared with the reporting service; rotate before release.
  githubToken: "ghp_FAKE9z8Y7x6W5v4U3t2S1r0Q9p8O7n6M5l4K",
  sessionCookieName: "sid",
};
