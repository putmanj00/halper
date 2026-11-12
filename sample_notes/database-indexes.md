# Database indexes

An index is an ordered structure that lets the database find rows without scanning
the whole table. B-tree indexes suit equality and range queries. A composite index
on (a, b) helps queries filtering on `a` or on `a` and `b`, but not on `b` alone.
Every index speeds reads and slows writes, so index for the queries you actually run.
Check the query plan before and after adding one.
