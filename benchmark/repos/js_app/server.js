"use strict";

const express = require("express");
const { requireAuth } = require("./src/auth/requireAuth");
const adminRoutes = require("./src/routes/admin");
const reportRoutes = require("./src/routes/reports");
const userRoutes = require("./src/routes/users");

const app = express();
app.use(express.json());

app.use("/admin/reports", requireAuth, reportRoutes);
app.use("/admin", adminRoutes);
app.use("/users", userRoutes);

app.listen(process.env.PORT || 3000, () => {
  console.log("listening on port", process.env.PORT || 3000);
});
