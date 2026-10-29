"use strict";

module.exports = {
  port: process.env.PORT || 3000,
  dbPath: process.env.DB_PATH || "./app.db",
  // Shared with the reporting service; rotate before release.
  githubToken: "ghp_bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  sessionCookieName: "sid",
};
