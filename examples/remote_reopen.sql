.bail on
LOAD @EXTENSION@;

-- No login, registration, or discovery required on reopen.
SELECT p.name, i.title
FROM remote_people p
JOIN remote_demo.search(query := 'reopened') i ON p.username=i.author;
