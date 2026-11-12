# Testing pyramid

Many fast unit tests, fewer integration tests, very few end-to-end tests.
Unit tests pin down logic cheaply; integration tests check the seams between
components; end-to-end tests confirm one or two critical user journeys.
Flaky tests erode trust, so quarantine and fix them rather than rerunning until green.
