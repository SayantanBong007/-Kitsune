"""Ground-truth questions for Kitsune benchmark v2.

Each question has ONE exact expected answer: (file basename, symbol name, chunk_type).
`alts` lists other acceptable triples (e.g. an equivalent method in another file).
Questions are paraphrased so the exact symbol name is NOT used verbatim.
"""

C, F = "class", "function"


def q(text, file, name, ctype, alts=None):
    return {"q": text, "expect": (file, name, ctype), "alts": alts or []}


GSON = [
    q("Which interface lets users decide which fields or classes are skipped during serialization?", "ExclusionStrategy.java", "ExclusionStrategy", C),
    q("Which interface translates a Java field into the JSON property name?", "FieldNamingStrategy.java", "FieldNamingStrategy", C),
    q("Which enum offers built-in field naming styles such as upper camel case or snake case?", "FieldNamingPolicy.java", "FieldNamingPolicy", C),
    q("Which utility turns a JSON string into a tree of elements?", "JsonParser.java", "JsonParser", C),
    q("Which class represents a JSON null value in the element tree?", "JsonNull.java", "JsonNull", C),
    q("Which element type holds named members, like a JSON object?", "JsonObject.java", "JsonObject", C),
    q("Which element type holds an ordered list of JSON elements?", "JsonArray.java", "JsonArray", C),
    q("Which class wraps a string, number or boolean inside the JSON tree?", "JsonPrimitive.java", "JsonPrimitive", C),
    q("Which interface do I implement to write custom logic that reads a JSON element into a Java type?", "JsonDeserializer.java", "JsonDeserializer", C),
    q("Which interface do I implement to write custom logic that turns a Java object into a JSON element?", "JsonSerializer.java", "JsonSerializer", C),
    q("Which interface creates instances of a type that has no no-argument constructor?", "InstanceCreator.java", "InstanceCreator", C),
    q("Which enum controls whether long values are written as numbers or as strings?", "LongSerializationPolicy.java", "LongSerializationPolicy", C),
    q("Which class configures newline and indentation when writing pretty JSON?", "FormattingStyle.java", "FormattingStyle", C),
    q("Which interface chooses how JSON numbers become Java Number objects?", "ToNumberStrategy.java", "ToNumberStrategy", C),
    q("Which class lets you block reflective access to selected classes?", "ReflectionAccessFilter.java", "ReflectionAccessFilter", C),
    q("Which parser reads several JSON values one after another from a stream?", "JsonStreamParser.java", "JsonStreamParser", C),
    q("Which internal class implements version, modifier and transient field exclusion rules?", "Excluder.java", "Excluder", C),
    q("Which internal class figures out how to build instances of a given class?", "ConstructorConstructor.java", "ConstructorConstructor", C),
    q("Which internal map keeps insertion order and backs the members of a JSON object?", "LinkedTreeMap.java", "LinkedTreeMap", C),
    q("Which factory binds JSON properties to Java fields using reflection?", "ReflectiveTypeAdapterFactory.java", "ReflectiveTypeAdapterFactory", C),
    q("Which method converts a Java object into a JSON string?", "Gson.java", "toJson", F),
    q("Which method parses JSON text into an object of a given type?", "Gson.java", "fromJson", F),
    q("Which method returns the type adapter for a type and caches it?", "Gson.java", "getAdapter", F),
    q("Which method converts a Java object into a tree of JSON elements?", "Gson.java", "toJsonTree", F),
    q("Which method wraps a Reader into a JsonReader using the configured leniency?", "Gson.java", "newJsonReader", F),
    q("Which builder method turns on indented, human-readable output?", "GsonBuilder.java", "setPrettyPrinting", F),
    q("Which builder method stops characters like < and > from being escaped in output?", "GsonBuilder.java", "disableHtmlEscaping", F),
    q("Which builder method only includes fields that carry the Expose annotation?", "GsonBuilder.java", "excludeFieldsWithoutExposeAnnotation", F),
    q("Which builder method registers a custom adapter, serializer or deserializer for a type?", "GsonBuilder.java", "registerTypeAdapter", F),
    q("Which reader method returns the next JSON token as a string?", "JsonReader.java", "nextString", F,
      alts=[("JsonTreeReader.java", "nextString", F)]),
]

PETCLINIC = [
    q("Which superclass holds the generated numeric id shared by all entities?", "BaseEntity.java", "BaseEntity", C),
    q("Which base class adds a name property to entities?", "NamedEntity.java", "NamedEntity", C),
    q("Which base class holds a first name and last name for a person?", "Person.java", "Person", C),
    q("Which entity represents a pet owner with address and telephone?", "Owner.java", "Owner", C),
    q("Which entity represents a pet with a birth date and type?", "Pet.java", "Pet", C),
    q("Which entity represents the kind of pet, such as cat or dog?", "PetType.java", "PetType", C),
    q("Which entity records a visit with a date and description?", "Visit.java", "Visit", C),
    q("Which entity represents a veterinarian with specialties?", "Vet.java", "Vet", C),
    q("Which entity represents a veterinary specialty?", "Specialty.java", "Specialty", C),
    q("Which class wraps the list of vets so it can be returned as a collection?", "Vets.java", "Vets", C),
    q("Which repository interface finds owners by last name?", "OwnerRepository.java", "OwnerRepository", C),
    q("Which repository interface finds the available pet types?", "PetTypeRepository.java", "PetTypeRepository", C),
    q("Which repository interface loads vets?", "VetRepository.java", "VetRepository", C),
    q("Which formatter converts between a pet type object and its text name?", "PetTypeFormatter.java", "PetTypeFormatter", C),
    q("Which configuration class sets up the cache for the application?", "CacheConfiguration.java", "CacheConfiguration", C),
    q("Which configuration class handles web settings such as locale?", "WebConfiguration.java", "WebConfiguration", C),
    q("Which class validates a pet's name, type and birth date?", "PetValidator.java", "PetValidator", C),
    q("Which controller deliberately throws an exception to demo error handling?", "CrashController.java", "CrashController", C),
    q("Which controller serves the home page?", "WelcomeController.java", "WelcomeController", C),
    q("Which method handles the form submission that creates a new owner?", "OwnerController.java", "processCreationForm", F),
    q("Which method shows the form to search for owners?", "OwnerController.java", "initFindForm", F),
    q("Which method handles the form submission that updates an existing owner?", "OwnerController.java", "processUpdateOwnerForm", F),
    q("Which method provides the list of pet types to the pet forms?", "PetController.java", "populatePetTypes", F),
    q("Which method shows the form to add a new pet?", "PetController.java", "initCreationForm", F),
    q("Which method works out the earliest allowed date for a visit?", "VisitController.java", "minVisitDate", F),
    q("Which method saves a newly submitted visit?", "VisitController.java", "processNewVisitForm", F),
    q("Which method checks a pet and records validation errors?", "PetValidator.java", "validate", F),
    q("Which method returns the list of vets for display?", "VetController.java", "showVetList", F),
    q("Which method provides the locale resolver bean?", "WebConfiguration.java", "localeResolver", F),
    q("Which method customizes the cache configuration for the petclinic cache?", "CacheConfiguration.java", "petclinicCacheConfigurationCustomizer", F),
]
