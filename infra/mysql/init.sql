CREATE TABLE IF NOT EXISTS patients (
    patient_id VARCHAR(36) PRIMARY KEY,
    full_name VARCHAR(120) NOT NULL,
    date_of_birth DATE NOT NULL
);
INSERT IGNORE INTO patients VALUES ('DEMO-001', 'Alex Demo', '1990-01-15');

CREATE TABLE IF NOT EXISTS appointment_slots (
    slot_id VARCHAR(36) PRIMARY KEY,
    doctor_name VARCHAR(100) NOT NULL,
    slot_date DATE NOT NULL,
    slot_time TIME NOT NULL,
    status ENUM('AVAILABLE', 'BOOKED') NOT NULL DEFAULT 'AVAILABLE',
    patient_name VARCHAR(120),
    patient_dob DATE,
    booked_at TIMESTAMP NULL,
    booking_key VARCHAR(100) UNIQUE,
    INDEX idx_date_status (slot_date, status)
);
INSERT IGNORE INTO appointment_slots (slot_id, doctor_name, slot_date, slot_time) VALUES
('SLOT-101', 'Dr. Patel', DATE_ADD(CURRENT_DATE, INTERVAL 1 DAY), '09:30:00'),
('SLOT-102', 'Dr. Patel', DATE_ADD(CURRENT_DATE, INTERVAL 1 DAY), '11:00:00'),
('SLOT-103', 'Dr. Smith', DATE_ADD(CURRENT_DATE, INTERVAL 1 DAY), '14:15:00'),
('SLOT-201', 'Dr. Smith', DATE_ADD(CURRENT_DATE, INTERVAL 2 DAY), '10:00:00');

CREATE TABLE IF NOT EXISTS prescriptions (
    prescription_id VARCHAR(36) PRIMARY KEY,
    patient_id VARCHAR(36) NOT NULL,
    medication VARCHAR(120) NOT NULL,
    status ENUM('READY', 'REQUESTED') NOT NULL,
    request_key VARCHAR(100) UNIQUE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (patient_id) REFERENCES patients(patient_id)
);
INSERT IGNORE INTO prescriptions (prescription_id, patient_id, medication, status)
VALUES ('RX-DEMO-001', 'DEMO-001', 'Metformin', 'READY');

CREATE TABLE IF NOT EXISTS call_audit_logs (
    audit_id BIGINT AUTO_INCREMENT PRIMARY KEY,
    call_id VARCHAR(36) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    details JSON NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_call (call_id, created_at)
);
