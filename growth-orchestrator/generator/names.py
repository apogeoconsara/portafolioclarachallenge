"""Fictional people / companies / titles. Nothing here is a real person or a registered brand."""
from __future__ import annotations

FIRST_ES = ["Alejandro", "Andrea", "Camila", "Carlos", "Carolina", "Claudia", "Daniel", "Diana", "Diego",
            "Elena", "Emilio", "Fernanda", "Gabriel", "Gabriela", "Guillermo", "Héctor", "Isabel", "Javier",
            "Jorge", "José", "Juan", "Julián", "Karla", "Laura", "Leticia", "Luis", "Manuel", "Marcela",
            "María", "Mariana", "Mauricio", "Mónica", "Natalia", "Nicolás", "Óscar", "Pablo", "Patricia",
            "Paula", "Rafael", "Ricardo", "Roberto", "Rocío", "Santiago", "Sebastián", "Sofía", "Tomás",
            "Valentina", "Verónica", "Víctor", "Ximena", "Adriana", "Andrés", "Beatriz", "César", "Dulce",
            "Esteban", "Francisco", "Ignacio", "Lorena", "Miguel", "Pilar", "Raúl", "Sergio", "Teresa"]
LAST_ES = ["García", "Rodríguez", "Martínez", "Hernández", "López", "González", "Pérez", "Sánchez", "Ramírez",
           "Torres", "Flores", "Rivera", "Gómez", "Díaz", "Reyes", "Morales", "Cruz", "Ortiz", "Gutiérrez",
           "Chávez", "Ramos", "Vargas", "Castillo", "Jiménez", "Moreno", "Romero", "Herrera", "Medina",
           "Aguilar", "Castro", "Vázquez", "Mendoza", "Ruiz", "Fernández", "Soto", "Rojas", "Silva",
           "Contreras", "Guerrero", "Salazar", "Navarro", "Domínguez", "Vega", "Peña", "Cárdenas", "Ibarra",
           "Valdés", "Paredes", "Lara", "Montoya", "Quintero", "Arias", "Cortés", "Escobar", "Pineda",
           "Zamora", "Bravo", "Ponce", "Acosta", "Maldonado"]
FIRST_PT = ["Ana", "Bruno", "Camila", "Carlos", "Daniela", "Eduardo", "Felipe", "Fernanda", "Gabriel",
            "Isabela", "João", "Juliana", "Larissa", "Leonardo", "Lucas", "Luiza", "Marcelo", "Mariana",
            "Mateus", "Patrícia", "Paulo", "Rafael", "Renata", "Rodrigo", "Tatiana", "Thiago", "Vanessa",
            "Vinícius", "Beatriz", "Cláudio"]
LAST_PT = ["Silva", "Santos", "Oliveira", "Souza", "Rodrigues", "Ferreira", "Alves", "Pereira", "Lima",
           "Gomes", "Costa", "Ribeiro", "Martins", "Carvalho", "Almeida", "Lopes", "Soares", "Fernandes",
           "Vieira", "Barbosa", "Rocha", "Dias", "Nascimento", "Andrade", "Moreira", "Nunes", "Marques",
           "Machado", "Mendes", "Freitas"]
FIRST_EN = ["Alex", "Daniel", "David", "Emily", "James", "Jessica", "Michael", "Rachel", "Sarah", "Thomas",
            "Andrew", "Laura", "Mark", "Nicole", "Peter"]

SDR_NAMES = ["Valeria Montes", "Andrés Quiroga", "Lucía Barrientos", "Mateo Salinas"]  # fictional Clara SDRs

STEMS = ["al", "ta", "vi", "mer", "cu", "ro", "sol", "ka", "nex", "lu", "min", "do", "ver", "ri", "pan", "gi",
         "tra", "bel", "fo", "ren", "sa", "mar", "pi", "lo", "cen", "tor", "yra", "ba", "qui", "zu", "ne",
         "va", "dor", "ten", "ma", "ci", "gal", "per", "ni", "to", "ar", "em", "or", "ul", "ix", "ca", "jo",
         "fer", "lis", "mo"]

# function -> seniority -> titles
TITLES = {
    "finance": {"c_level": ["CFO", "Chief Financial Officer"], "vp": ["VP of Finance"],
                "director": ["Director of Finance", "Director of Treasury", "Controller"],
                "manager": ["Finance Manager", "Treasury Manager", "Accounts Payable Manager"],
                "ic": ["Financial Analyst", "Accounts Payable Analyst", "Accountant"]},
    "procurement": {"c_level": ["Chief Procurement Officer"], "vp": ["VP of Procurement"],
                    "director": ["Director of Procurement", "Director of Sourcing"],
                    "manager": ["Procurement Manager"], "ic": ["Procurement Analyst", "Buyer"]},
    "operations": {"c_level": ["COO"], "vp": ["VP of Operations"], "director": ["Director of Operations"],
                   "manager": ["Operations Manager"], "ic": ["Operations Coordinator"]},
    "it": {"c_level": ["CTO"], "vp": ["VP of Technology"], "director": ["Director of IT"],
           "manager": ["IT Manager"], "ic": ["Systems Analyst"]},
    "hr": {"c_level": ["CHRO"], "vp": ["VP of People"], "director": ["Director of People"],
           "manager": ["HR Manager"], "ic": ["HR Analyst"]},
    "executive": {"c_level": ["CEO", "General Manager", "Founder"], "vp": ["Executive VP"],
                  "director": ["Associate General Manager"], "manager": ["Executive Assistant"],
                  "ic": ["Executive Assistant"]},
    "other": {"c_level": ["Chief Administrative Officer"], "vp": ["VP of Administration"],
              "director": ["Director of Administration"], "manager": ["Administration Manager"],
              "ic": ["Administrative Assistant"]},
}

ERPS = ["SAP Business One", "Oracle NetSuite", "Contpaqi", "Siigo", "Totvs", "Defontana", "Bsale", "Odoo"]
PRODUCTS = ["an online ordering platform", "a new premium product line", "a mobile app for customers",
            "a 24-hour delivery service", "a sustainable product line", "a loyalty program"]
LOST_REASONS = ["price", "chose current bank", "no budget", "no response", "timing"]
