# HTTP caching basics

A cache stores a response so later requests can skip the origin server.
The `Cache-Control` header sets how long a response stays fresh (`max-age`) and
who may store it (`public`, `private`). Validators such as `ETag` let a client
ask "has this changed?" and receive a cheap `304 Not Modified` instead of the body.
Cache invalidation is hard; prefer short lifetimes plus validators over clever purging.
